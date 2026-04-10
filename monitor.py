import os
import re
import json
import time
import schedule
import requests
import anthropic
from datetime import datetime

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PRODUCTS          = [p.strip() for p in os.environ["PRODUCT"].split(",")]
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

# Mémoire : {product: {site: {"found": bool, "in_stock": bool}}}
previous_states = {}

def fetch_page(site: str) -> str:
    if SCRAPER_API_KEY and ("bigw.com.au" in site or "ebgames.com.au" in site):
        scraper_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={site}"
        resp = requests.get(scraper_url, timeout=60)
    else:
        resp = requests.get(site, headers=HEADERS, timeout=40)
    return resp.text

def analyse_site(product: str, site: str, page_text: str) -> dict:
    site = site.strip()
    domain = site.replace("https://", "").replace("http://", "").split("/")[0]

    prompt = f"""Voici le contenu texte d'une page de recherche sur {domain}.
Produit recherché : "{product}"
{f"Conditions : {CONDITIONS}" if CONDITIONS else ""}

Contenu de la page :
{page_text}

Analyse si le produit "{product}" est référencé sur cette page et s'il est disponible en stock.
Réponds UNIQUEMENT avec ce JSON (sans markdown, sans backticks) :
{{"found": true ou false, "in_stock": true ou false, "summary": "1-2 phrases sur ce que tu as trouvé", "price": "prix si trouvé ou null", "url": "{site}", "confidence": "high/medium/low"}}

Note: "found" = le produit est référencé sur le site. "in_stock" = le produit est disponible à l'achat maintenant (pas épuisé, pas "coming soon", pas "notify me").
Si found est false, in_stock doit aussi être false."""

    try:
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
            return {"site": domain, "found": False, "in_stock": False, "error": "JSON non parseable", "raw_url": site}
        parsed["site"] = domain
        parsed["raw_url"] = site
        return parsed

    except Exception as e:
        return {"site": domain, "found": False, "in_stock": False, "error": str(e), "raw_url": site}

def send_discord_alert(product: str, new_listings: list, back_in_stock: list):
    if not new_listings and not back_in_stock:
        return
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    embeds = []

    for r in new_listings:
        price = r.get("price") or "N/A"
        url = r.get("url") or r.get("raw_url", "")
        stock_status = "En stock" if r.get("in_stock") else "Référencé (stock inconnu)"
        embeds.append({
            "title": f"Nouveau listing sur {r['site']} !",
            "description": r.get("summary", ""),
            "color": 3066993,
            "fields": [
                {"name": "Produit", "value": product, "inline": True},
                {"name": "Prix", "value": price, "inline": True},
                {"name": "Disponibilité", "value": stock_status, "inline": True},
                {"name": "Lien", "value": url, "inline": False},
            ],
            "footer": {"text": f"Détecté le {now}"}
        })

    for r in back_in_stock:
        price = r.get("price") or "N/A"
        url = r.get("url") or r.get("raw_url", "")
        embeds.append({
            "title": f"De retour en stock sur {r['site']} !",
            "description": r.get("summary", ""),
            "color": 15844367,
            "fields": [
                {"name": "Produit", "value": product, "inline": True},
                {"name": "Prix", "value": price, "inline": True},
                {"name": "Lien", "value": url, "inline": False},
            ],
            "footer": {"text": f"Détecté le {now}"}
        })

    parts = []
    if new_listings:
        parts.append(f"Nouveau listing sur {len(new_listings)} site(s)")
    if back_in_stock:
        parts.append(f"De retour en stock sur {len(back_in_stock)} site(s)")

    payload = {
        "content": f"**{product}** — {' | '.join(parts)} !",
        "embeds": embeds
    }

    resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    if resp.status_code in (200, 204):
        print(f"  Alerte Discord envoyée pour {product}")
    else:
        print(f"  Erreur Discord : {resp.status_code} — {resp.text}")

def run_scan():
    global previous_states
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"Scan démarré : {now}")
    print(f"Produits     : {len(PRODUCTS)}")
    print(f"Sites        : {len(SITES)}")
    print(f"{'='*55}")

    pages = {}
    for site in SITES:
        site = site.strip()
        domain = site.replace("https://", "").replace("http://", "").split("/")[0]
        print(f"  Chargement : {domain} ...", end=" ", flush=True)
        try:
            html = fetch_page(site)
            text_only = re.sub(r"<[^>]+>", " ", html)
            text_only = re.sub(r"\s+", " ", text_only).strip()[:6000]
            pages[site] = text_only
            print("OK")
        except requests.exceptions.Timeout:
            pages[site] = None
            print("TIMEOUT")
        except Exception as e:
            pages[site] = None
            print(f"ERREUR — {e}")
        time.sleep(1)

    for product in PRODUCTS:
        print(f"\n--- Produit : {product} ---")
        if product not in previous_states:
            previous_states[product] = {}

        new_listings = []
        back_in_stock = []

        for site in SITES:
            site = site.strip()
            domain = site.replace("https://", "").replace("http://", "").split("/")[0]
            page_text = pages.get(site)

            if not page_text:
                print(f"  {domain} ... ignoré")
                continue

            print(f"  {domain} ...", end=" ", flush=True)
            r = analyse_site(product, site, page_text)

            currently_found = r.get("found", False)
            currently_in_stock = r.get("in_stock", False)
            prev = previous_states[product].get(domain, {"found": False, "in_stock": False})
            previously_found = prev.get("found", False)
            previously_in_stock = prev.get("in_stock", False)

            if r.get("error"):
                print(f"ERREUR — {r['error']}")
            elif currently_found:
                stock_txt = "EN STOCK" if currently_in_stock else "référencé (épuisé)"
                print(f"TROUVÉ — {stock_txt} — {r.get('summary','')}")
            else:
                print("absent")

            # Nouveau listing
            if currently_found and not previously_found:
                print(f"    → NOUVEAU référencement !")
                new_listings.append(r)
            # Retour en stock
            elif currently_in_stock and not previously_in_stock and previously_found:
                print(f"    → DE RETOUR EN STOCK !")
                back_in_stock.append(r)

            previous_states[product][domain] = {
                "found": currently_found,
                "in_stock": currently_in_stock
            }
            time.sleep(1)

        if new_listings or back_in_stock:
            send_discord_alert(product, new_listings, back_in_stock)
        else:
            print(f"  Pas de changement pour {product}")

if __name__ == "__main__":
    print(f"Surveillance démarrée — {len(PRODUCTS)} produit(s) — scan toutes les {INTERVAL_MINUTES} minutes")
    run_scan()
    schedule.every(INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
