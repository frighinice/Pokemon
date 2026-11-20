"""
Carica nel database Supabase espansioni, carte e prezzi del giorno da TCGdex.

Da lanciare una volta al giorno (a mano o in automatico). Ogni esecuzione:
  - aggiunge le espansioni nuove (uscite dalla DATA_INIZIO in poi)
  - aggiorna le carte (nomi, rarità, immagini)
  - salva il prezzo di oggi di ogni carta (lo storico si costruisce così)

Configurazione: crea nella stessa cartella un file ".env" con queste due righe
(oppure imposta le stesse variabili d'ambiente):

    SUPABASE_URL=https://xxxxxxxx.supabase.co
    SUPABASE_SERVICE_KEY=sb_secret_...   (la "Secret key": NON condividerla)

Uso:
    python carica_catalogo.py              carica tutto
    python carica_catalogo.py --prova      scarica i dati ma non scrive nel database

Richiede solo Python 3.8+ (nessuna libreria da installare).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

API = "https://api.tcgdex.net/v2"
DATA_INIZIO = "2026-07-17"          # uscita di Pitch Black: si caricano i set da qui in poi
SET_SEMPRE = ["me05", "30th", "me06"]  # Pitch Black, 30th Celebration, Delta Reign
SET_DA_CONTROLLARE = 40             # quanti set recenti esaminare per trovare quelli nuovi
HEADERS = {"User-Agent": "catalogo-pokemon/1.0"}


# ---------------------------------------------------------------- utilità

def carica_env():
    env = Path(__file__).with_name(".env")
    if env.exists():
        for riga in env.read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if riga and not riga.startswith("#") and "=" in riga:
                k, v = riga.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_json(url, tentativi=3):
    """Scarica JSON da TCGdex. None se la risorsa non esiste (404)."""
    for i in range(tentativi):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tentativi - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if i == tentativi - 1:
                raise
        time.sleep(1.5 * (i + 1))
    return None


def numero(x):
    try:
        return round(float(x), 2) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


class Supabase:
    def __init__(self, url, chiave):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {
            "apikey": chiave,   # le nuove chiavi sb_secret_ vanno solo in questo header
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }

    def upsert(self, tabella, righe, chiave_conflitto, blocco=500):
        """Inserisce o aggiorna le righe (se esistono già, le sovrascrive)."""
        for i in range(0, len(righe), blocco):
            corpo = json.dumps(righe[i:i + blocco]).encode("utf-8")
            url = f"{self.base}{tabella}?on_conflict={chiave_conflitto}"
            req = urllib.request.Request(url, data=corpo, headers=self.headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60):
                    pass
            except urllib.error.HTTPError as e:
                dettaglio = e.read().decode("utf-8", "replace")
                raise SystemExit(f"Errore Supabase su '{tabella}' ({e.code}): {dettaglio}")


# ---------------------------------------------------------------- TCGdex

def trova_espansioni():
    """Set da caricare: quelli fissi + quelli recenti usciti da DATA_INIZIO in poi."""
    elenco = get_json(f"{API}/en/sets") or []
    candidati = list(dict.fromkeys(SET_SEMPRE + [s["id"] for s in elenco[-SET_DA_CONTROLLARE:]]))
    scelte = []
    for set_id in candidati:
        en = get_json(f"{API}/en/sets/{set_id}")
        it = get_json(f"{API}/it/sets/{set_id}")
        dettaglio = it or en
        if not dettaglio:
            continue
        uscita = (en or {}).get("releaseDate") or dettaglio.get("releaseDate")
        if set_id in SET_SEMPRE or (uscita and uscita >= DATA_INIZIO):
            scelte.append((set_id, it, en, uscita))
    return scelte


def prezzi_da(card):
    cm = ((card or {}).get("pricing") or {}).get("cardmarket") or {}
    trend, trend_holo, avg30 = numero(cm.get("trend")), numero(cm.get("trend-holo")), numero(cm.get("avg30"))
    return {
        "riferimento": next((p for p in (trend, trend_holo, avg30) if p), None),
        "trend": trend,
        "trend_holo": trend_holo,
        "avg30": avg30,
        "aggiornato_cardmarket": cm.get("updated") or None,
    }


def scarica_carta(args):
    card_id, set_id = args
    it = get_json(f"{API}/it/cards/{card_id}")
    en = get_json(f"{API}/en/cards/{card_id}")
    base = it or en or {}
    prezzi = prezzi_da(it)
    if prezzi["riferimento"] is None:
        prezzi = prezzi_da(en)
    img_it, img_en = (it or {}).get("image"), (en or {}).get("image")
    carta = {
        "id": card_id,
        "espansione_id": set_id,
        "numero": base.get("localId", ""),
        "nome_it": (it or {}).get("name"),
        "nome_en": (en or {}).get("name"),
        "rarita": (it or {}).get("rarity") or (en or {}).get("rarity"),
        "immagine_url": f"{img_it or img_en}/high.webp" if (img_it or img_en) else None,
        "immagine_lingua": "it" if img_it else ("en" if img_en else None),
    }
    return carta, prezzi


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prova", action="store_true", help="non scrive nel database")
    a = p.parse_args()

    carica_env()
    url, chiave = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not a.prova and not (url and chiave):
        sys.exit("Mancano SUPABASE_URL e/o SUPABASE_SERVICE_KEY (file .env o variabili d'ambiente).")
    db = None if a.prova else Supabase(url, chiave)

    oggi = date.today().isoformat()
    print("Cerco le espansioni su TCGdex...")
    espansioni = trova_espansioni()
    if not espansioni:
        sys.exit("Nessuna espansione trovata.")

    righe_esp, righe_carte, righe_prezzi = [], [], []
    for set_id, it, en, uscita in espansioni:
        dettaglio = it or en
        righe_esp.append({
            "id": set_id,
            "nome_it": (it or {}).get("name"),
            "nome_en": (en or {}).get("name"),
            "data_uscita": uscita,
            "totale_carte": ((dettaglio.get("cardCount") or {}).get("total")) or len(dettaglio.get("cards", [])),
        })
        ids = [(c["id"], set_id) for c in dettaglio.get("cards", [])]
        nome = (it or {}).get("name") or (en or {}).get("name") or set_id
        print(f"  {nome} [{set_id}]: {len(ids)} carte...")
        with ThreadPoolExecutor(max_workers=5) as ex:
            for carta, prezzi in ex.map(scarica_carta, ids):
                righe_carte.append(carta)
                righe_prezzi.append({"carta_id": carta["id"], "data": oggi, **prezzi})

    con_prezzo = sum(1 for r in righe_prezzi if r["riferimento"] is not None)
    print(f"\nTotale: {len(righe_esp)} espansioni, {len(righe_carte)} carte, {con_prezzo} con prezzo.")

    if a.prova:
        print("Modalità prova: niente scritto nel database.")
        return

    print("Scrivo nel database...")
    db.upsert("espansioni", righe_esp, "id")
    db.upsert("carte", righe_carte, "id")
    db.upsert("prezzi", righe_prezzi, "carta_id,data")
    print("Fatto.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        sys.exit(f"Errore di rete: {e}. Controlla la connessione e riprova.")
