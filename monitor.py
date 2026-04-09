import os
import re
import json
import time
import schedule
import requests
import anthropic
from datetime import datetime

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PRODUCT           = os.environ["PRODUCT"]
SITES             = os.environ["SITES"].split(",")
CONDITIONS        = os.environ.get("CONDITIONS", "")
DISCORD_WEBHOOK   = os.environ["DISCORD_WEBHOOK"]
INTERVAL_MINUTES  = int(os.environ.get("INTERVAL_MINUTES", "60"))
SCRAPER_API_KEY   = os.environ.get("SCRAPER_API_KEY", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9,fr;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

previous_states = {}

def fetch_page(site: str) -> str:
    if SCRAPER_API_KEY and ("bigw.com.au" in site or "ebgames.com.au" in site):
        scraper_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={site}"
        resp = requests.get(scraper_url, timeout=60)
    else:
        resp = requests.get(site, headers=HEADERS, timeout=40)
    return resp.text

def analyse_site(site: str) -> dict:
    site = site.strip()
    domain = site.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        html = fetch_page(site)
        text_only = re.sub(r"<[^>]+>", " ", html)
        text_only = re.sub(r"\s+", " ", text_only).strip()[:6000]

        prompt = f"""Voici le contenu texte d'une page de recherche sur {domain}.
Produit recherché : "{PRODUCT}"
{f"Conditions : {CONDITIONS}" if CONDITIONS else ""}

Contenu de la page :
{text_only}

Analyse si le produit "{PRODUCT}" est référencé sur cette page.
Réponds UNIQUEMENT avec ce JSON (sans markdown, sans backticks) :
{{"found": true ou false, "summary": "1-2 phrases sur ce que tu as trouvé", "price": "prix si trouvé ou null", "url": "{site}", "confidence": "high/medium/low"}}"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )

        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        parsed = None
        for i in range(len(text)):
            if text[i] == "{":
                for j in range(len(text), i, -1):
                    if text[j-1] == "}":
                        try:
                            parsed = json.loads(text[i:j])
                            break
                        except Exception:
                            continue
            if parsed:
                break

        if not parsed:
            return {"site": domain, "found": False, "error": "JSON non parseable", "raw_url": site}
        parsed["site"] = domain
        parsed["raw_url"] = site
        return parsed

    except requests.exceptions.Timeout:
        return {"site": domain, "found": False, "error": "Timeout", "raw_url": site}
    except Exception as e:
        return {"site": domain, "found": False, "error": str(e), "raw_url": site}

def send_discord_alert(new_findings: list):
    if not new_findings:
        return
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")

    embeds = []
    for r in new_findings:
        price = r.get("price") or "N/A"
        url = r.get("url") or r.get("raw_url", "")
        embeds.append({
            "title": f"Nouveau listing sur {r['site']} !",
            "description": r.get("summary", ""),
            "color": 3066993,
            "fields": [
                {"name": "Prix", "value": price, "inline": True},
                {"name": "Site", "value": r["site"], "inline": True},
                {"name": "Lien", "value": url, "inline": False},
            ],
            "footer": {"text": f"Détecté le {now}"}
        })

    payload = {
        "content": f"**Nouveau référencement détecté — {PRODUCT}** sur {len(new_findings)} site(s) !",
        "embeds": embeds
    }

    resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    if resp.status_code in (200, 204):
        print(f"Alerte Discord envoyée — {len(new_findings)} nouveau(x) référencement(s)")
    else:
        print(f"Erreur Discord : {resp.status_code} — {resp.text}")

def run_scan():
    global previous_states
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"Scan démarré : {now}")
    print(f"Produit      : {PRODUCT}")
    print(f"Sites        : {len(SITES)}")
    print(f"{'='*55}")

    results = []
    new_findings = []

    for site in SITES:
        site = site.strip()
        print(f"  Analyse : {site} ...", end=" ", flush=True)
        r = analyse_site(site)
        results.append(r)
        domain = r["site"]
        currently_found = r.get("found", False)
        previously_found = previous_states.get(domain, False)

        if r.get("error"):
            print(f"ERREUR — {r['error']}")
        elif currently_found:
            print(f"TROUVÉ — {r.get('summary','')}")
        else:
            print(f"absent")

        if currently_found and not previously_found:
            print(f"    → NOUVEAU référencement sur {domain} !")
            new_findings.append(r)

        previous_states[domain] = currently_found
        time.sleep(2)

    found_count = sum(1 for r in results if r.get("found"))
    print(f"\nRésultat : {found_count}/{len(results)} sites avec le produit")
    print(f"Nouveaux : {len(new_findings)} nouveau(x) référencement(s)")

    if new_findings:
        print("Envoi de l'alerte Discord...")
        send_discord_alert(new_findings)
    else:
        print("Pas de nouveau référencement — pas d'alerte envoyée")

if __name__ == "__main__":
    print(f"Surveillance démarrée — scan toutes les {INTERVAL_MINUTES} minutes")
    run_scan()
    schedule.every(INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
