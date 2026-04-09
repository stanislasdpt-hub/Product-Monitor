import os
import json
import time
import smtplib
import schedule
import anthropic
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─── Configuration (via variables d'environnement) ───────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PRODUCT           = os.environ["PRODUCT"]            # ex: "iPhone 16 Pro 256Go"
SITES             = os.environ["SITES"].split(",")   # ex: "https://amazon.fr,https://fnac.com"
CONDITIONS        = os.environ.get("CONDITIONS", "") # ex: "en stock, prix < 900€"
EMAIL_TO          = os.environ["EMAIL_TO"]
EMAIL_FROM        = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD    = os.environ["EMAIL_PASSWORD"]     # mot de passe app Gmail
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
INTERVAL_MINUTES  = int(os.environ.get("INTERVAL_MINUTES", "60"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Analyse d'un site via Claude + web search ───────────────────────────────

def analyse_site(site: str) -> dict:
    site = site.strip()
    try:
        domain = site.replace("https://", "").replace("http://", "").split("/")[0]
        prompt = f"""Tu es un assistant de veille e-commerce.
Recherche si le produit "{PRODUCT}" est actuellement référencé sur le site {site}.
{f"Conditions supplémentaires : {CONDITIONS}" if CONDITIONS else ""}

Utilise la recherche web pour vérifier la présence de ce produit sur ce site précis.
Réponds UNIQUEMENT avec un objet JSON valide, sans markdown ni backticks :
{{"found": true ou false, "summary": "1-2 phrases", "price": "prix ou null", "url": "URL directe ou null", "confidence": "high/medium/low"}}"""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )

        text = "".join(b.text for b in response.content if hasattr(b, "text"))

        # Tentative 1 : extraire le JSON brut
        parsed = None
        for start in range(len(text)):
            if text[start] == "{":
                for end in range(len(text), start, -1):
                    if text[end-1] == "}":
                        try:
                            parsed = json.loads(text[start:end])
                            break
                        except Exception:
                            continue
            if parsed:
                break

        # Tentative 2 : demander une reformulation si pas de JSON
        if not parsed:
            retry = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[
                    {"role": "user", "content": f"Voici une analyse de produit : {text[:1000]}\n\nRéponds UNIQUEMENT avec ce JSON (sans markdown) : {{\"found\": true/false, \"summary\": \"résumé\", \"price\": null, \"url\": null, \"confidence\": \"low\"}}"}
                ]
            )
            retry_text = "".join(b.text for b in retry.content if hasattr(b, "text"))
            for start in range(len(retry_text)):
                if retry_text[start] == "{":
                    for end in range(len(retry_text), start, -1):
                        if retry_text[end-1] == "}":
                            try:
                                parsed = json.loads(retry_text[start:end])
                                break
                            except Exception:
                                continue
                if parsed:
                    break

        if not parsed:
            return {"site": domain, "found": False, "error": "Réponse non parseable", "summary": text[:200]}

        parsed["site"] = domain
        parsed["raw_url"] = site
        return parsed

    except Exception as e:
        domain = site.replace("https://", "").split("/")[0]
        return {"site": domain, "found": False, "error": str(e)}

# ─── Envoi d'alerte email ────────────────────────────────────────────────────

def send_alert(results: list):
    found = [r for r in results if r.get("found")]
    if not found:
        return

    now = datetime.now().strftime("%d/%m/%Y à %H:%M")

    rows = ""
    for r in found:
        price = r.get("price") or "N/A"
        url   = r.get("url") or r.get("raw_url", "")
        link  = f'<a href="{url}" style="color:#185FA5">{url[:60]}{"..." if len(url)>60 else ""}</a>' if url else "—"
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-weight:500">{r['site']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;color:#3B6D11">{price}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee">{r.get('summary','')}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;font-size:12px">{link}</td>
        </tr>"""

    html = f"""
    <div style="font-family:sans-serif;max-width:700px;margin:auto">
      <div style="background:#EAF3DE;border-left:4px solid #639922;padding:16px 20px;border-radius:4px;margin-bottom:24px">
        <strong style="color:#27500A">Produit détecté !</strong>
        <p style="color:#3B6D11;margin:4px 0 0">{PRODUCT} trouvé sur {len(found)} site(s) — {now}</p>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <thead>
          <tr style="background:#f5f5f5">
            <th style="padding:10px 12px;text-align:left">Site</th>
            <th style="padding:10px 12px;text-align:left">Prix</th>
            <th style="padding:10px 12px;text-align:left">Résumé</th>
            <th style="padding:10px 12px;text-align:left">Lien</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin-top:24px">
        Alerte générée automatiquement · Prochain scan dans {INTERVAL_MINUTES} min
      </p>
    </div>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Alerte] {PRODUCT} détecté sur {len(found)} site(s)"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Email envoyé — {len(found)} détection(s)")

# ─── Cycle de scan complet ────────────────────────────────────────────────────

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
        status = "TROUVÉ" if r.get("found") else ("ERREUR" if r.get("error") else "absent")
        print(status)
        if r.get("found"):
            print(f"    Prix    : {r.get('price', 'N/A')}")
            print(f"    Résumé  : {r.get('summary', '')}")
        time.sleep(2)

    found_count = sum(1 for r in results if r.get("found"))
    print(f"\nRésultat : {found_count}/{len(results)} sites avec le produit")

    if found_count > 0:
        print("Envoi de l'alerte email...")
        send_alert(results)
    else:
        print("Aucune détection — pas d'email envoyé")

# ─── Point d'entrée ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Surveillance démarrée — scan toutes les {INTERVAL_MINUTES} minutes")
    run_scan()  # scan immédiat au démarrage
    schedule.every(INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
