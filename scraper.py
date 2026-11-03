"""Scarica i calendari degli organizzatori, li fa leggere a un'AI e salva data/events.json."""
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import anthropic
import requests
from bs4 import BeautifulSoup

MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
MAX_CHARS = 60000
OUT = "data/events.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PisteLibereBot/1.0)"}

# Per aggiungere un organizzatore basta aggiungere una riga qui.
SITES = [
    {"organizer": "Rossocorsa", "url": "https://www.rossocorsaonline.com/prove"},
    {"organizer": "Gully Racing", "url": "https://www.gullyracing.it/calendario", "keep_repeats": True},
    {"organizer": "Rehm Race Days", "url": "https://www.rehmracedays.com/it/rehm-race-calendario/"},
    {"organizer": "Promoracing", "url": "https://www.promoracing.it/it/calendario/moto"},
    {"organizer": "Actionbike", "url": "https://www.actionbike.de/en/booking"},
]

PROMPT = """Oggi è {today}. Ti passo il testo estratto dalla pagina calendario di un organizzatore di giornate in pista per moto ({organizer}).
Estrai TUTTI gli eventi (prove libere, prove cronometrate, race weekend, ecc.) in un array JSON.
Rispondi SOLO con JSON valido, senza testo prima o dopo e senza backtick.

Ogni evento ha questi campi:
- "circuit": solo il nome del circuito, SENZA numerazioni o numeri di edizione dell'evento (es. "Aragon I" e "Aragon II" diventano entrambi "Aragon")
- "date_start": data di inizio, formato YYYY-MM-DD
- "date_end": data di fine, formato YYYY-MM-DD (uguale a date_start se è un solo giorno)
- "event_type": breve etichetta come nella pagina (es. "Prove cronometrate", "Race weekend")
- "prices": lista di {{"label": testo, "amount_eur": numero}} con i prezzi indicati; lista vuota se non ce ne sono
- "availability": "disponibile", "lista_attesa", "esaurito" oppure "non_indicata"
- "url": link specifico dell'evento se presente nel testo (tra parentesi quadre), altrimenti null
- "notes": info utili non coperte dagli altri campi (max 150 caratteri), oppure null

Regole:
- Non inventare nulla. Se un dato non è nella pagina usa null (lista vuota per prices, "non_indicata" per availability).
- Se l'anno non è scritto nel testo, deducilo dalla data di oggi: scegli il primo anno in cui quel giorno e mese cadono oggi o in futuro; se l'elenco passa da dicembre a gennaio, incrementa l'anno.
- Se un prezzo ha un secondo valore tra parentesi e il significato non è spiegato, inseriscilo con label "prezzo alternativo (significato non indicato)".
- Se ci sono più tariffe per lo stesso evento (es. 1, 2 o 3 giorni), inserisci una voce di prezzo per ciascuna con label chiara.
- Se il sito elenca ogni giorno come riga separata, mantieni un evento per riga.

TESTO DELLA PAGINA:
{text}
"""

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AVAILABILITY = {"disponibile", "lista_attesa", "esaurito", "non_indicata"}


def page_to_text(html, base_url, keep_repeats=False):
    """Toglie il codice, tiene il testo e i link, scarta le righe ripetute (rumore)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    # Alcuni siti (Gully) mettono il link dell'evento in un attributo onclick invece che in un <a>.
    for tag in soup.find_all(onclick=True):
        m = re.search(r"['\"](/[^'\"]+|https?://[^'\"]+)['\"]", tag["onclick"])
        if m and "print" not in m.group(1):
            tag.append(f" [{urljoin(base_url, m.group(1))}] ")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        a.replace_with(f"{a.get_text(' ', strip=True)} [{urljoin(base_url, href)}]")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text("\n").splitlines()]
    lines = [ln for ln in lines if ln]
    if not keep_repeats:
        counts = Counter(lines)
        lines = [ln for ln in lines if counts[ln] <= 5]
    return "\n".join(lines)[:MAX_CHARS]


def extract(client, organizer, text, today):
    prompt = PROMPT.format(today=today.isoformat(), organizer=organizer, text=text)
    msg = client.messages.create(
        model=MODEL, max_tokens=12000, messages=[{"role": "user", "content": prompt}]
    )
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("risposta dell'AI troncata")
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError("l'AI non ha restituito una lista")
    return data


def clean_event(ev, site, today):
    """Controlla un evento estratto dall'AI. Restituisce None se non è valido o è già passato."""
    try:
        start = ev["date_start"]
        end = ev.get("date_end") or start
        if not (DATE_RE.match(start) and DATE_RE.match(end)):
            return None
        d_start, d_end = date.fromisoformat(start), date.fromisoformat(end)
        if d_end < d_start or d_end < today:
            return None
        circuit = (ev.get("circuit") or "").strip()
        circuit = re.sub(r"\s+(?:[IVX]{1,4}|\d{1,2})$", "", circuit).strip()  # toglie "II", "2" finali
        if not circuit:
            return None
        prices = []
        for p in ev.get("prices") or []:
            amount = p.get("amount_eur")
            if isinstance(amount, (int, float)) and amount > 0:
                prices.append({"label": str(p.get("label") or "").strip(), "amount_eur": amount})
        url = ev.get("url")
        if not (isinstance(url, str) and url.startswith("http")):
            url = site["url"]
        availability = ev.get("availability")
        return {
            "organizer": site["organizer"],
            "circuit": circuit,
            "date_start": start,
            "date_end": end,
            "event_type": (ev.get("event_type") or "").strip() or None,
            "prices": prices,
            "availability": availability if availability in AVAILABILITY else "non_indicata",
            "url": url,
            "notes": (ev.get("notes") or None),
        }
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("events", []), data.get("sources", {})
    except (OSError, ValueError):
        return [], {}


def main():
    today = date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old_events, old_sources = load_previous()
    client = anthropic.Anthropic()
    events, sources, failures = [], {}, 0

    for site in SITES:
        org = site["organizer"]
        try:
            resp = requests.get(site["url"], headers=HEADERS, timeout=30)
            resp.raise_for_status()
            if resp.encoding and resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            text = page_to_text(resp.text, site["url"], site.get("keep_repeats", False))
            raw = extract(client, org, text, today)
            found = [e for e in (clean_event(x, site, today) for x in raw) if e]
            if not found:
                raise RuntimeError("0 eventi trovati: controlla la pagina o il testo estratto")
            events += found
            sources[org] = {"ok": True, "count": len(found), "last_success": now}
            print(f"[OK] {org}: {len(found)} eventi")
        except Exception as exc:  # un sito che fallisce non deve bloccare gli altri
            failures += 1
            kept = []
            for e in old_events:
                if e["organizer"] == org and e["date_end"] >= today.isoformat():
                    kept.append(e)
            events += kept
            sources[org] = {
                "ok": False,
                "error": str(exc)[:200],
                "count": len(kept),
                "last_success": old_sources.get(org, {}).get("last_success"),
            }
            print(f"[ERRORE] {org}: {exc}")

    unique = {}
    for e in events:
        key = (e["organizer"], e["date_start"], e["date_end"], e["circuit"].lower(), (e["event_type"] or "").lower())
        unique[key] = e
    events = sorted(unique.values(), key=lambda e: (e["date_start"], e["organizer"]))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "sources": sources, "events": events}, f, ensure_ascii=False, indent=1)
    print(f"Salvati {len(events)} eventi in {OUT}")
    if failures == len(SITES):
        sys.exit(1)


if __name__ == "__main__":
    main()
