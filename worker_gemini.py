import os
import re
import unicodedata
import time
import json
import requests
from google import genai  # SDK baru: google-genai


# ============================
# KONFIG
# ============================
JOBS_API_URL = "https://leamarie-yoga.de/jobs_api.php"  # GANTI ke URL jobs_api.php kamu

MIN_SECONDS_PER_REQUEST = 8
MAX_RETRIES_PER_TITLE = 3
DEFAULT_QUOTA_SLEEP_SECONDS = 60


# ============================
# FUNGSI BANTUAN
# ============================
def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text)
    text = text.strip("-")
    text = text.lower()
    return text or "article"


def parse_retry_delay_seconds(err_str: str) -> float:
    m = re.search(r"retry in ([0-9.]+)s", err_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return DEFAULT_QUOTA_SLEEP_SECONDS


def build_prompt(judul: str) -> str:
    judul_safe = judul.replace('"', "'")
    prompt = f"""
ABSOLUTELY NO <h1> TAG ALLOWED. START WITH <p> OR OUTPUT IS USELESS.

You are a professional SEO content writer.. 
Your articles regularly hit position #1–3 on Google because they are helpful, authoritative, and feel genuinely human.

Main title to write about: "{judul_safe}"

Your task:
Write one complete, high-quality SEO article in English that perfectly satisfies Google’s E-E-A-T guidelines.

Do these steps internally (never show them in the output):
1. Create 10 alternative, more clickable title variations (for your reference only).
2. Build a logical, value-packed outline with at least 7–9 H2 sections before FAQ & Conclusion.
3. Research/recall the most recent 2024–2025 data, statistics, tools, or trends related to the topic.

STRICT WRITING RULES YOU MUST FOLLOW:
- Write in a warm, conversational yet authoritative tone — like a trusted expert talking directly to the reader.
- Use “you” frequently to make it personal and engaging.
- Naturally weave in real-world experience or observations.
- Use smooth transitions (however, here’s the thing, the good news is, interestingly, for example, etc.).
- Keep passive voice under 8%.
- Avoid keyword stuffing — use the main keyword and related terms naturally.
- Every section must deliver real value; no fluff.
- When using lists, make them numbered H3s (1., 2., 3…) and explain each item in depth.
- Include up-to-date facts, statistics, tools, or case studies where relevant.
- Opening paragraph: instantly engaging, data-rich or insight-rich, no rhetorical questions.

REQUIRED STRUCTURE:
- Strong introduction
- Logical H2 sections
- Use numbered <h3> for lists inside sections
- End with exactly these two sections:
  <h2>FAQ</h2>
  <h2>Conclusion</h2>

OUTPUT FORMAT:
1. ONLY the clean article HTML (no <html>, <head>, or <body>).
2. After the HTML, add one blank line, lalu:
   META_DESC: your compelling meta description (145–160 characters, plain text, no quotes)

Now write the best possible article for this title:
"{judul_safe}"
"""
    return prompt


# ============================
# LOAD API KEY DARI ENV + WORKER_INDEX
# ============================

# WORKER_INDEX tetap dipakai buat logging / pembagian job
worker_index_str = os.getenv("WORKER_INDEX", "0")
try:
    WORKER_INDEX = int(worker_index_str)
except ValueError:
    raise ValueError(f"WORKER_INDEX bukan integer valid: {worker_index_str}")

# Ambil raw secret: boleh berisi 1 atau banyak API key (dipisah newline)
raw_api = os.getenv("GEMINI_API_KEY", "").strip()
if not raw_api:
    raise ValueError(
        "Environment variable GEMINI_API_KEY tidak ditemukan / kosong. "
        "Pastikan sudah diset di GitHub Secrets dan dipasang di YAML."
    )

# Pecah per baris → jadi list API key
api_keys = [line.strip() for line in raw_api.splitlines() if line.strip()]

if not api_keys:
    raise ValueError(
        "GEMINI_API_KEY ter-set tapi tidak ada API key valid (semua kosong?)."
    )

if WORKER_INDEX < 0 or WORKER_INDEX >= len(api_keys):
    raise IndexError(
        f"WORKER_INDEX={WORKER_INDEX} di luar range. "
        f"Hanya tersedia {len(api_keys)} API key di secret GEMINI_API_KEY."
    )

API_KEY = api_keys[WORKER_INDEX]

print(
    f"🔑 Worker index {WORKER_INDEX} pakai API key ke-{WORKER_INDEX+1} "
    f"dari total {len(api_keys)} key. Prefix: {API_KEY[:8]}..."
)

client = genai.Client(api_key=API_KEY)


# ============================
# AMBIL JOB DARI SERVER
# ============================
def get_next_job(max_retries: int = 5):
    """
    Ambil 1 job dari server.
    - Return dict job     → kalau sukses
    - Return None         → kalau server bilang 'no_job' (benar-benar habis)
    - Return "RETRY"      → kalau error sementara (500, network, dll)
    """
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(JOBS_API_URL, params={"action": "next"}, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[JOB] ❌ Error ambil job (attempt {attempt}): {e}")
            # Error koneksi / HTTP 500 / timeout → tunggu sebentar lalu coba lagi
            time.sleep(5)
            continue

        # Kalau server balas ok = False, lihat reason
        if not data.get("ok"):
            reason = data.get("reason")
            if reason == "no_job":
                print("[JOB] ✅ Server bilang: tidak ada job pending.")
                return None  # benar-benar habis
            else:
                print("[JOB] ⚠ Response tidak OK:", data)
                # Anggap error sementara, coba lagi
                time.sleep(5)
                continue

        # Sukses ambil job
        return data.get("job")

    # Sudah coba beberapa kali tapi tetap gagal → suruh caller RETRY nanti
    print("[JOB] ❌ Gagal ambil job setelah beberapa attempt. Tidur 60 detik lalu coba lagi.")
    time.sleep(60)
    return "RETRY"


# ============================
# KIRIM HASIL KE SERVER
# ============================
def submit_result(job_id, status, judul=None, slug=None, metadesc=None, artikel=None):
    payload = {"job_id": job_id, "status": status}

    if status == "done":
        payload.update({
            "judul": judul,
            "slug": slug,
            "metadesc": metadesc,
            "artikel": artikel,
        })

    try:
        r = requests.post(
            JOBS_API_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=60
        )
        r.raise_for_status()
        print(f"[JOB {job_id}] 📤 POST sukses:", r.json())
    except Exception as e:
        print(f"[JOB {job_id}] ❌ Error submit: {e}")


# ============================
# MAIN LOOP
# ============================
def main():
    last_call = 0.0
    total_sukses = 0

    while True:
        job = get_next_job()

        # Benar-benar tidak ada job lagi (server bilang no_job)
        if job is None:
            print("\n🎉 Tidak ada job lagi. Worker berhenti (no_job dari server).")
            break

        # Error sementara saat ambil job → lanjut loop, coba ambil lagi
        if job == "RETRY":
            continue

        # Di titik ini, job seharusnya dict berisi data job
        job_id = job["id"]
        judul = job["keyword"]
        print(f"\n[JOB {job_id}] 🎯 Judul: {judul} (worker {WORKER_INDEX})")

        success = False

        for attempt in range(1, MAX_RETRIES_PER_TITLE + 1):
            try:
                elapsed = time.time() - last_call
                if elapsed < MIN_SECONDS_PER_REQUEST:
                    time.sleep(MIN_SECONDS_PER_REQUEST - elapsed)

                print(f"[JOB {job_id}] 🔄 Gemini request (attempt {attempt})")

                prompt = build_prompt(judul)
                res = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                )
                last_call = time.time()

                raw = (res.text or "").strip()
                if not raw:
                    print(f"[JOB {job_id}] ⚠ Output kosong dari Gemini.")
                    break

                # META_DESC parsing
                m = re.search(r"META_DESC\s*:(.*)$", raw, re.IGNORECASE | re.DOTALL)
                if m:
                    metadesc = m.group(1).strip()
                    artikel_html = raw[: m.start()].strip()
                else:
                    print(f"[JOB {job_id}] ⚠ META_DESC tidak ditemukan, generate dari artikel.")
                    artikel_html = raw
                    txt = re.sub(r"<.*?>", " ", artikel_html)
                    txt = re.sub(r"\s+", " ", txt).strip()
                    metadesc = txt[:155]

                if not artikel_html:
                    print(f"[JOB {job_id}] ⚠ Artikel kosong setelah parsing.")
                    break

                slug = slugify(judul)

                submit_result(
                    job_id=job_id,
                    status="done",
                    judul=judul,
                    slug=slug,
                    metadesc=metadesc,
                    artikel=artikel_html,
                )

                total_sukses += 1
                print(f"[JOB {job_id}] ✅ DONE. Total sukses: {total_sukses}")
                success = True
                break

            except Exception as e:
                err_str = str(e)
                low = err_str.lower()
                print(f"[JOB {job_id}] ❌ Error Gemini: {err_str}")

                # Kalau key ketahuan leaked / permission denied → jangan retry terus
                if "reported as leaked" in low or "permission_denied" in low:
                    print(f"[JOB {job_id}] ⛔ API key bermasalah (leaked/permission). Stop worker.")
                    submit_result(job_id=job_id, status="failed")
                    return

                if "quota" in low or "limit" in low or "exceeded" in low:
                    delay = parse_retry_delay_seconds(err_str)
                    print(f"[JOB {job_id}] 🚫 Quota/limit → tidur {delay:.1f}s lalu coba lagi.")
                    time.sleep(delay)
                    continue

                print(f"[JOB {job_id}] ⚠ Error lain → sleep 10 detik lalu retry.")
                time.sleep(10)

        if not success:
            print(f"[JOB {job_id}] ❌ Gagal permanen setelah {MAX_RETRIES_PER_TITLE} attempt.")
            submit_result(job_id=job_id, status="failed")

    print(f"\n🎉 Worker index {WORKER_INDEX} selesai. Total artikel sukses: {total_sukses}")


if __name__ == "__main__":
    main()
