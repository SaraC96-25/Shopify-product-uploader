import io
import os
import zipfile
import base64
import time
from datetime import datetime
from typing import List, Dict, Tuple, Optional

import requests
import streamlit as st

# =========================
# Streamlit — Shopify Uploader
# =========================
# Requisiti (aggiungili al tuo requirements.txt):
#   streamlit
#   requests
#   Pillow
#
# Come funziona:
# - Carica un archivio .zip che contenga N sottocartelle
#   Ogni sottocartella rappresenta un *modello* e contiene esattamente 2 immagini (consigliato: JPG/PNG/WebP)
# - Il nome della cartella = nome modello (es. "AURORA-01")
# - Le immagini saranno ordinate per *data di modifica* (metadati del file nello zip): più vecchia prima, più nuova dopo
# - Per ogni cartella/modello viene creato un prodotto su Shopify con titolo:
#       "SOLAR SCREEN® Pellicole Decorative - #Modello#"
#   con descrizione fissa (HTML) e assegnato alle collezioni:
#       "Homepage", "Pellicole per Vetri", "Decorative"
# - Le immagini vengono caricate usando l'API Admin (base64) con position 1..n secondo l'ordinamento sopra
# - Se una collezione non esiste, verrà creata come *custom collection*
#
# Sicurezza: usa un token Admin API privato (Storefront token NON va bene). Inseriscilo nei secrets di Streamlit Cloud.
#
# NOTE su limiti:
# - Lo zip deve preservare i timestamp di modifica dei file; in caso manchino, useremo l'ordine alfabetico come fallback.
# - Assicura che le cartelle contengano solo 2 immagini ciascuna.

DESCRIPTION_HTML = (
    "Il desiderio di privacy è del tutto normale... Sia in azienda che nel comfort della propria casa, permette di ritrovarsi ed essere se stessi.\n\n"
    "Con la nostra gamma di pellicole design, si aprono nuove possibilità di privacy per coloro&nbsp;che ne hanno bisogno, che si tratti di una sala riunioni o una stanza da bagno.\n\n"
    "Le nostre pellicole offrono diversi livelli di opacità e differenti motivi, per corrispondere meglio alle esigenze di privacy, senza diminuire di luminosità.\n\n"
    "Ispirandosi alla città, alla natura, alle forme geometriche o al cielo, sono disponibili decine di modelli colorati od opachi per rivestimento, che si possono adattare ai vetri di imprese e negozi: pellicole opalescenti, sfumate, bianche, grigie o nere, occultanti o colorate. La pellicola Aurora con tecnologia dicroica offre un gioco di luci impercettibile.\n\n"
    "Originalità garantita!"
)

DEFAULT_COLLECTIONS = ["Homepage", "Pellicole per Vetri", "Decorative"]

# ---------- Helpers ----------

def human_ts(dt_tuple) -> float:
    """Converte (Y, M, D, h, m, s) di ZipInfo in epoch seconds."""
    try:
        return time.mktime(datetime(*dt_tuple).timetuple())
    except Exception:
        return 0.0


def is_image(filename: str) -> bool:
    fname = filename.lower()
    return fname.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))


def parse_zip_models(zf: zipfile.ZipFile) -> List[Dict]:
    """Estrae la struttura Modello -> [immagini ordinate] dallo zip.
    Ritorna una lista di dict: {model: str, images: List[dict], warnings: List[str]}
    """
    folders: Dict[str, List[zipfile.ZipInfo]] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        if not is_image(info.filename):
            continue
        parts = info.filename.split("/")
        if len(parts) < 2:
            # immagini nella root: ignoriamo
            continue
        folder = parts[0].strip()
        folders.setdefault(folder, []).append(info)

    models = []
    for folder, infos in folders.items():
        warnings = []
        # ordina per mtime (più vecchia prima), fallback alfabetico
        try:
            infos_sorted = sorted(infos, key=lambda i: human_ts(i.date_time))
        except Exception:
            infos_sorted = sorted(infos, key=lambda i: i.filename)
            warnings.append("Timestamp non disponibili: ordinamento alfabetico usato.")

        # vincolo: 2 immagini
        img_infos = [i for i in infos_sorted if is_image(i.filename)]
        if len(img_infos) != 2:
            warnings.append(f"La cartella '{folder}' contiene {len(img_infos)} immagini (attese: 2). Verranno usate le prime 2.")
            img_infos = img_infos[:2]

        images_payload = []
        for pos, info in enumerate(img_infos, start=1):
            raw = zf.read(info)
            b64 = base64.b64encode(raw).decode("utf-8")
            images_payload.append({
                "attachment": b64,
                "filename": os.path.basename(info.filename),
                "position": pos,
            })

        models.append({
            "model": folder,
            "images": images_payload,
            "warnings": warnings,
        })
    return models


# ---------- Shopify API ----------

class ShopifyClient:
    def __init__(self, shop_domain: str, access_token: str, api_version: str = "2024-10"):
        # api_version può essere regolato; usa una versione recente supportata dal tuo store
        self.base = f"https://{shop_domain}/admin/api/{api_version}"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ---- Helpers ----
    def _scrub_variants(self, variants: List[Dict]) -> List[Dict]:
        clean = []
        drop_keys = {"id", "product_id", "admin_graphql_api_id", "position", "created_at", "updated_at", "image_id"}
        for v in variants or []:
            nv = {k: v[k] for k in v.keys() if k not in drop_keys}
            clean.append(nv)
        return clean

    def _scrub_options(self, options: List[Dict]) -> List[Dict]:
        clean = []
        drop_keys = {"id", "product_id", "admin_graphql_api_id", "position"}
        for o in options or []:
            no = {k: o[k] for k in o.keys() if k not in drop_keys}
            clean.append(no)
        return clean

    # ---- Collections ----
    def _find_custom_collection(self, title: str) -> Optional[int]:
        r = self.session.get(f"{self.base}/custom_collections.json", params={"title": title, "limit": 1})
        r.raise_for_status()
        data = r.json().get("custom_collections", [])
        return data[0]["id"] if data else None

    def _find_smart_collection(self, title: str) -> Optional[int]:
        r = self.session.get(f"{self.base}/smart_collections.json", params={"title": title, "limit": 1})
        r.raise_for_status()
        data = r.json().get("smart_collections", [])
        return data[0]["id"] if data else None

    def ensure_collection(self, title: str) -> int:
        # tenta smart poi custom; se non esiste, crea custom
        # attenzione speciale per la Home page (handle frontpage)
        if title.strip().lower() in {"homepage", "home page", "frontpage", "home"}:
            # prova a trovare la collezione di default con handle frontpage
            r = self.session.get(f"{self.base}/custom_collections.json", params={"handle": "frontpage", "limit": 1})
            r.raise_for_status()
            data = r.json().get("custom_collections", [])
            if data:
                return data[0]["id"]
        cid = self._find_smart_collection(title)
        if cid:
            return cid
        cid = self._find_custom_collection(title)
        if cid:
            return cid
        # crea custom collection
        payload = {"custom_collection": {"title": title}}
        r = self.session.post(f"{self.base}/custom_collections.json", json=payload)
        r.raise_for_status()
        return r.json()["custom_collection"]["id"]

    def add_product_to_collection(self, product_id: int, collection_id: int):
        payload = {"collect": {"product_id": product_id, "collection_id": collection_id}}
        r = self.session.post(f"{self.base}/collects.json", json=payload)
        r.raise_for_status()
        return r.json().get("collect")

    def list_product_collections(self, product_id: int) -> List[int]:
        r = self.session.get(f"{self.base}/collects.json", params={"product_id": product_id, "limit": 250})
        r.raise_for_status()
        collects = r.json().get("collects", [])
        return [c.get("collection_id") for c in collects]

    # ---- Products ----
    def get_product_by_id(self, pid: int) -> Optional[Dict]:
        r = self.session.get(f"{self.base}/products/{pid}.json")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("product")

    def get_product_by_handle(self, handle: str) -> Optional[Dict]:
        r = self.session.get(f"{self.base}/products.json", params={"handle": handle, "limit": 1})
        r.raise_for_status()
        arr = r.json().get("products", [])
        return arr[0] if arr else None

    def get_product_by_id_or_handle(self, ident: str) -> Optional[Dict]:
        ident = ident.strip()
        if ident.isdigit():
            return self.get_product_by_id(int(ident))
        return self.get_product_by_handle(ident)

    def create_product(self, title: str, body_html: str, images_payload: List[Dict], template: Optional[Dict] = None) -> Dict:
        product_body = {
            "title": title,
            "body_html": body_html,
            "images": images_payload,
        }
        if template:
            # copia campi utili dal template (senza id/handle)
            product_body.update({
                "vendor": template.get("vendor"),
                "product_type": template.get("product_type"),
                "tags": template.get("tags"),
                "options": self._scrub_options(template.get("options")),
                "variants": self._scrub_variants(template.get("variants")),
            })
        payload = {"product": product_body}
        r = self.session.post(f"{self.base}/products.json", json=payload)
        r.raise_for_status()
        return r.json()["product"]
        # api_version può essere regolato; usa una versione recente supportata dal tuo store
        self.base = f"https://{shop_domain}/admin/api/{api_version}"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ---- Collections ----
    def _find_custom_collection(self, title: str) -> Optional[int]:
        r = self.session.get(f"{self.base}/custom_collections.json", params={"title": title, "limit": 1})
        r.raise_for_status()
        data = r.json().get("custom_collections", [])
        return data[0]["id"] if data else None

    def _find_smart_collection(self, title: str) -> Optional[int]:
        r = self.session.get(f"{self.base}/smart_collections.json", params={"title": title, "limit": 1})
        r.raise_for_status()
        data = r.json().get("smart_collections", [])
        return data[0]["id"] if data else None

    def ensure_collection(self, title: str) -> int:
        # tenta smart poi custom; se non esiste, crea custom
        # attenzione speciale per la Home page (handle frontpage)
        if title.strip().lower() in {"homepage", "home page", "frontpage", "home"}:
            # prova a trovare la collezione di default con handle frontpage
            r = self.session.get(f"{self.base}/custom_collections.json", params={"handle": "frontpage", "limit": 1})
            r.raise_for_status()
            data = r.json().get("custom_collections", [])
            if data:
                return data[0]["id"]
        cid = self._find_smart_collection(title)
        if cid:
            return cid
        cid = self._find_custom_collection(title)
        if cid:
            return cid
        # crea custom collection
        payload = {"custom_collection": {"title": title}}
        r = self.session.post(f"{self.base}/custom_collections.json", json=payload)
        r.raise_for_status()
        return r.json()["custom_collection"]["id"]

    def add_product_to_collection(self, product_id: int, collection_id: int):
        payload = {"collect": {"product_id": product_id, "collection_id": collection_id}}
        r = self.session.post(f"{self.base}/collects.json", json=payload)
        r.raise_for_status()
        return r.json().get("collect")

    # ---- Products ----
    def create_product(self, title: str, body_html: str, images_payload: List[Dict]) -> Dict:
        product = {
            "product": {
                "title": title,
                "body_html": body_html,
                "images": images_payload,
                # opzionale: puoi impostare vendor, product_type, status
                # "status": "active"
            }
        }
        r = self.session.post(f"{self.base}/products.json", json=product)
        r.raise_for_status()
        return r.json()["product"]


# ---------- UI ----------

st.set_page_config(page_title="Shopify Decorative Uploader", page_icon="🛒", layout="wide")

st.title("🛒 Caricatore prodotti Shopify — Pellicole Decorative")
st.write("Carica uno **ZIP** con sottocartelle (ognuna con 2 immagini). Il nome della cartella è il *modello*.")

with st.sidebar:
    st.header("Connessione Shopify")
    default_domain = st.secrets.get("SHOP_DOMAIN", "")
    default_token = st.secrets.get("SHOP_ACCESS_TOKEN", "")
    default_version = st.secrets.get("SHOP_API_VERSION", "2024-10")

    shop_domain = st.text_input("Shop domain (es. my-store.myshopify.com)", value=default_domain)
    access_token = st.text_input("Admin API access token", value=default_token, type="password")
    api_version = st.text_input("API version", value=default_version)

    st.divider()
    st.caption("I valori possono essere gestiti come *secrets* su Streamlit Cloud.")

    st.subheader("Modalità")
    duplication_enabled = st.toggle("Duplica da prodotto esistente", value=False, help="Copia varianti, vendor, product type, tag e descrizione dal prodotto sorgente; titolo e immagini verranno sostituiti.")
    source_identifier = ""
    copy_source_collections = False
    if duplication_enabled:
        source_identifier = st.text_input("ID o handle del prodotto sorgente", placeholder="es. 1234567890 o pellicola-aurora-01")
        copy_source_collections = st.checkbox("Copia le collezioni dal prodotto sorgente", value=True, help="Se attivo, ignora l'elenco predefinito e riusa le stesse collezioni del sorgente.")

uploaded = st.file_uploader("Carica archivio .zip", type=["zip"], accept_multiple_files=False)

if uploaded is not None:
    # parse zip
    try:
        zf = zipfile.ZipFile(io.BytesIO(uploaded.read()))
    except zipfile.BadZipFile:
        st.error("Archivio zip non valido.")
        st.stop()

    models = parse_zip_models(zf)

    if not models:
        st.warning("Nessuna cartella valida trovata nello zip. Assicurati che le immagini siano in sottocartelle.")
    else:
        st.success(f"Trovati {len(models)} modelli.")
        # preview
        for m in models:
            with st.expander(f"Modello: {m['model']} — {len(m['images'])} immagini"):
                if m["warnings"]:
                    for w in m["warnings"]:
                        st.warning(w)
                cols = st.columns(len(m["images"]) or 1)
                for col, img in zip(cols, m["images"]):
                    # mostra anteprima
                    raw = base64.b64decode(img["attachment"])  # bytes
                    col.image(raw, caption=f"{img['filename']} (pos {img['position']})", use_column_width=True)

        st.divider()
        start = st.button("🚀 Crea prodotti su Shopify", type="primary", disabled=not (shop_domain and access_token))

        if start:
            if not (shop_domain and access_token):
                st.error("Inserisci dominio e token nelle impostazioni a sinistra.")
                st.stop()

            client = ShopifyClient(shop_domain, access_token, api_version)

            # Se abilitata la duplicazione, recupera il prodotto sorgente
            template_product = None
            source_collection_ids: List[int] = []
            if duplication_enabled:
                if not source_identifier:
                    st.error("Modalità duplicazione: specifica un ID o handle del prodotto sorgente.")
                    st.stop()
                with st.spinner("Recupero prodotto sorgente..."):
                    template_product = client.get_product_by_id_or_handle(source_identifier)
                    if not template_product:
                        st.error("Prodotto sorgente non trovato.")
                        st.stop()
                    if copy_source_collections and template_product.get("id"):
                        source_collection_ids = client.list_product_collections(template_product["id"]) or []

            # Assicura le collezioni (se non copiamo dal sorgente)
            collection_ids: Dict[str, int] = {}
            if not (duplication_enabled and copy_source_collections):
                with st.spinner("Verifica/creazione collezioni..."):
                    for title in DEFAULT_COLLECTIONS:
                        try:
                            cid = client.ensure_collection(title)
                            collection_ids[title] = cid
                        except requests.HTTPError as e:
                            st.error(f"Errore collezione '{title}': {e}")
                            st.stop()

            # processa ogni modello
            results = []
            progress = st.progress(0.0, text="Creazione prodotti in corso...")
            for idx, m in enumerate(models, start=1):
                model_name = m["model"]
                title = f"SOLAR SCREEN® Pellicole Decorative - {model_name}"
                try:
                    # scegli la descrizione: se duplicazione, usa quella del sorgente, altrimenti quella predefinita
                    body = template_product.get("body_html") if duplication_enabled and template_product else DESCRIPTION_HTML
                    product = client.create_product(title=title, body_html=body, images_payload=m["images"], template=(template_product if duplication_enabled else None))
                    pid = product["id"]
                    # aggiungi alle collezioni
                    if duplication_enabled and copy_source_collections and source_collection_ids:
                        for cid in source_collection_ids:
                            client.add_product_to_collection(pid, cid)
                    else:
                        for ctitle, cid in collection_ids.items():
                            client.add_product_to_collection(pid, cid)
                    results.append({
                        "Modello": model_name,
                        "Product ID": pid,
                        "Titolo": product.get("title"),
                        "Handle": product.get("handle"),
                        "Status": product.get("status"),
                        "Collezioni": ", ".join(DEFAULT_COLLECTIONS),
                    })
                except requests.HTTPError as e:
                    try:
                        detail = e.response.json()
                    except Exception:
                        detail = {"error": str(e)}
                    results.append({
                        "Modello": model_name,
                        "Product ID": "—",
                        "Titolo": title,
                        "Handle": "—",
                        "Status": "ERRORE",
                        "Collezioni": ", ".join(DEFAULT_COLLECTIONS),
                        "Errore": detail,
                    })
                progress.progress(idx/len(models), text=f"{idx}/{len(models)} completati")

            st.success("Operazione terminata.")
            st.dataframe(results, use_container_width=True)

else:
    st.info("Carica uno zip per iniziare. Esempio struttura: 'AURORA-01/img1.jpg', 'AURORA-01/img2.jpg', ...")
