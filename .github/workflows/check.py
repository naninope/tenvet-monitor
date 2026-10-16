"""Sprawdza wolne terminy w systemie Vetineo i wysyła powiadomienie przez ntfy.

Ustawienia (zmienne środowiskowe, ustawiane w .github/workflows/monitor.yml):
  CLINIC_URL   - adres strony rezerwacji kliniki
  DOCTOR       - fragment nazwiska lekarza (np. "Jankowska")
  DATE_FROM / DATE_TO   - stały zakres dat RRRR-MM-DD (opcjonalnie)
  DAYS_FROM / DAYS_TO   - albo zakres przesuwny: od ilu do ilu dni od dziś
  MIN_HOUR / MAX_HOUR   - godziny rozpoczęcia wizyty (MAX_HOUR opcjonalnie)
  NTFY_TOPIC   - temat w aplikacji ntfy (sekret w GitHubie)
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CLINIC_URL = os.environ.get("CLINIC_URL", "https://tenvet-krakow.rezerwacja.vetineo.com/")
DOCTOR = os.environ.get("DOCTOR", "Jankowska")
MIN_HOUR = int(os.environ.get("MIN_HOUR") or 0)
MAX_HOUR = int(os.environ.get("MAX_HOUR") or 24)
TOPIC = os.environ["NTFY_TOPIC"].strip()
STATE_FILE = "state/seen.json"

today = datetime.now(ZoneInfo("Europe/Warsaw")).date()
if os.environ.get("DATE_FROM"):
    date_from = os.environ["DATE_FROM"]
    date_to = os.environ.get("DATE_TO") or date_from
else:
    date_from = str(today + timedelta(days=int(os.environ.get("DAYS_FROM") or 0)))
    date_to = str(today + timedelta(days=int(os.environ.get("DAYS_TO") or 30)))

if str(today) > date_to:
    print(f"Zakres {date_from} – {date_to} już minął, nic nie sprawdzam.")
    sys.exit(0)

JS_READ = """(doctor) => {
  const cd = window.clinicData;
  const wl = cd && cd.workHoursData && cd.workHoursData.whList;
  if (!wl || !Object.keys(wl).length) return null;
  const doc = cd.doctors.find(d => d.doctorName.toLowerCase().includes(doctor.toLowerCase()));
  if (!doc) return {error: 'Nie znaleziono lekarza: ' + doctor};
  const days = wl[doc.doctorId] || {};
  const free = [];
  for (const slots of Object.values(days))
    for (const s of slots) if (s.canBeBooked) free.push(s.startDateTime);
  return {name: doc.doctorName, free};
}"""


def read_slots():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")  # Chrome preinstalowany na GitHubie
        except Exception:
            browser = p.chromium.launch()
        page = browser.new_page(locale="pl-PL", timezone_id="Europe/Warsaw")
        page.goto(CLINIC_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function(
            "() => window.clinicData && window.clinicData.workHoursData"
            " && window.clinicData.workHoursData.whList"
            " && Object.keys(window.clinicData.workHoursData.whList).length",
            timeout=60000,
        )
        data = page.evaluate(JS_READ, DOCTOR)
        browser.close()
    if not data or data.get("error"):
        raise SystemExit((data or {}).get("error", "Nie udało się odczytać danych ze strony"))
    return data

def read_slots_retry(attempts=3):
  for attempt in range(1, attempts + 1):
    try:
      return read_slots()
    except PWTimeout as e:
      print(f"Proba {attempt}/{attempts} nieudana (timeout): {str(e).splitlines()[0]}")
      if attempt < attempts:
        time.sleep(20)
  print("::warning::Strona rezerwacji nie odpowiada - pomijam to sprawdzenie, kolejny przebieg sprobuje ponownie")
  sys.exit(0)


def notify(title, message, priority="urgent"):
    req = urllib.request.Request(
        "https://ntfy.sh/" + TOPIC,
        data=message.encode("utf-8"),
        headers={
            "Title": title,  # tylko znaki ASCII (bez polskich liter)
            "Priority": priority,
            "Tags": "dog,calendar",
            "Click": CLINIC_URL,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30)


def main():
    data = read_slots_retry()
    hits = sorted(
        s for s in data["free"]
        if date_from <= s[:10] <= date_to and MIN_HOUR <= int(s[11:13]) < MAX_HOUR
    )
    print(f"{data['name']} | {date_from} – {date_to} | {MIN_HOUR}:00–{MAX_HOUR}:00")
    print("Pasujące terminy:", ", ".join(hits) or "brak")

    os.makedirs("state", exist_ok=True)
    try:
        seen = set(json.load(open(STATE_FILE)))
    except Exception:
        seen = set()
    fresh = [h for h in hits if h not in seen]
    if fresh:
        notify(
            "Wolny termin TENVET!",
            f"{data['name']}:\n" + "\n".join(fresh) + "\n\nKliknij, aby otworzyć rezerwację.",
        )
        print("Wysłano powiadomienie:", fresh)
    # pamiętamy tylko terminy, które nadal są wolne – jeśli ktoś zwolni je ponownie, znów powiadomimy
    json.dump(hits, open(STATE_FILE, "w"))


if __name__ == "__main__":
    main()
