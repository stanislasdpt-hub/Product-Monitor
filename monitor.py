import os
import json
import time
import smtplib
import schedule
import requests
import anthropic
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PRODUCT           = os.environ["PRODUCT"]
SITES             = os.environ["SITES"].split(",")
CONDITIONS        = os.environ.get("CONDITIONS", "")
EMAIL_TO          = os.environ["EMAIL_TO"]
EMAIL_FROM        = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
INTERVAL_MINUTES  = int(os.environ.get("INTERVAL_MINUTES", "60"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

def analyse_site(site: str) -> dict:
    import re
    site = site.strip()
    domain = site.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        resp = requests.get(site, headers=HEADERS, timeout=20)
        html = resp.text
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

def send_alert(results):
    found = [r for r in results if r.get("found")]
    if not found:
        return
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    rows = ""
    for r in found:
        price = r.get("price") or "N/A"
        url = r.get("url") or r.get("raw_url", "")
        link = f'<a href="{url}">{url[:60]}</a>' if url else "—"
        rows += f"<tr><td>{r['site']}</td><td>{price}</td><td>{r.get('summary','')}</td><td>{link}</td></tr>"

    html = f"""<div style="font-family:sans-serif;max-width:700px;margin:auto">
      <div style="background:#EAF3DE;border-left:4px solid #639922;padding:16px;border-radius:4px;margin-bottom:24px">
        <strong style="color:#27500A">Produit détecté !</strong>
        <p style="color:#3B6D11;margin:4px 0 0">{PRODUCT} trouvé sur {len(found)} site(s) — {now}</p>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <thead><tr style="background:#f5f5f5"><th>Site</th><th>Prix</th><th>Résumé</th><th>Lien</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin-top:24px">Prochain scan dans {INTERVAL_MINUTES} min</p>
    </div>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Alerte] {PRODUCT} détecté sur {len(found)} site(s)"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"Email envoyé — {len(found)} détection(s)")

def run_scan():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*55}")
    print(f"Scan démarré : {now}")
    print(f"Produit      : {PRODUCT}")
    print(f"Sites        : {len(SITES)}")
    print(f"{'='*55}")
    results = []
    for site in SITES:
        print(f"  Analyse : {site.strip()} ...", end=" ", flush=True)
        r = analyse_site(site)
        results.append(r)
        if r.get("error"):
            print(f"ERREUR — {r['error']}")
        elif r.get("found"):
            print(f"TROUVÉ — {r.get('summary','')}")
        else:
            print(f"absent")
        time.sleep(2)
    found_count = sum(1 for r in results if r.get("found"))
    print(f"\nRésultat : {found_count}/{len(results)} sites avec le produit")
    if found_count > 0:
        send_alert(results)
    else:
        print("Aucune détection — pas d'email envoyé")

if __name__ == "__main__":
    print(f"Surveillance démarrée — scan toutes les {INTERVAL_MINUTES} minutes")
    run_scan()
    schedule.every(INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
