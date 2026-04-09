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
EMAIL_TO          = os.environ["EMAIL_TO"]
EMAIL_FROM        = os.environ["EMAIL_FROM"]
SENDGRID_API_KEY  = os.environ["SENDGRID_API_KEY"]
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
    if SCRAPER_API_KEY and "bigw.com.au" in site:
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

def send_alert(new_findings: list):
    if not new_findings:
        return
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    rows = ""
    for r in new_findings:
        price = r.get("price") or "N/A"
        url = r.get("url") or r.get("raw_url", "")
        link = f'<a href="{url}" style="color:#185FA5">{url[:60]}{"..." if len(url)>60 else ""}</a>' if url else "—"
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-weight:500">{r['site']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#3B6D11">{price}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee">{r.get('summary','')}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-size:12px">{link}</td>
        </tr>"""

    html = f"""<div style="font-family:sans-serif;max-width:700px;margin:auto">
      <div style="background:#EAF3DE;border-left:4px solid #639922;padding:16px;border-radius:4px;margin-bottom:24px">
        <strong style="color:#27500A">Nouveau référencement détecté !</strong>
        <p style="color:#3B6D11;margin:4px 0 0">{PRODUCT} vient d'apparaître sur {len(new_findings)} site(s) — {now}</p>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <thead><tr style="background:#f5f5f5">
          <th style="padding:10px 12px;text-align:left">Site</th>
          <th style="padding:10px 12px;text-align:left">Prix</th>
          <th style="padding:10px 12px;text-align:left">Résumé</th>
          <th style="padding:10px 12px;text-align:left">Lien</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin-top:24px">Prochain scan dans {INTERVAL_MINUTES} min</p>
    </div>"""

    payload = {
        "personalizations": [{"to": [{"email": EMAIL_TO}]}],
        "from": {"email": EMAIL_FROM},
        "subject": f"[Nouveau listing] {PRODUCT} sur {len(new_findings)} site(s)",
        "content": [{"type": "text/html", "value": html}]
    }

    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=15
    )

    if resp.status_code == 202:
        print(f"Email envoyé via SendGrid — {len(new_findings)} nouveau(x) référencement(s)")
    else:
        print(f"Erreur SendGrid : {resp.status_code} — {resp.text}")

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
        print("Envoi de l'alerte email via SendGrid...")
        send_alert(new_findings)
    else:
        print("Pas de nouveau référencement — pas d'email envoyé")

if __name__ == "__main__":
    print(f"Surveillance démarrée — scan toutes les {INTERVAL_MINUTES} minutes")
    run_scan()
    schedule.every(INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
