# -*- coding: utf-8 -*-
"""
Cautare automata licitatii - GREEN KRAFT  (versiunea web)

Ruleaza in GitHub Actions la fiecare 2 ore. Cauta pe sursele din surse.json,
clasifica rezultatele (cuvinte_cheie.json), le salveaza in Supabase (aplicatia web
le afiseaza) si trimite email cu anunturile NOI.

Variabile de mediu (GitHub > Settings > Secrets): SUPABASE_URL, SUPABASE_SERVICE_KEY,
SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PAROLA, APP_URL

Rulare locala:  python cautare_licitatii.py --test   (nu scrie nimic, face doar raport HTML)
                python cautare_licitatii.py --sursa rovigo,turmac --test
"""
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import traceback
import unicodedata
import warnings
import webbrowser
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import quote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

DIR = os.path.dirname(os.path.abspath(__file__))
F_SURSE = os.path.join(DIR, "surse.json")
F_CUVINTE = os.path.join(DIR, "cuvinte_cheie.json")
LOG = os.path.join(DIR, "date", "jurnal.txt")
RAPORT = os.path.join(DIR, "date", "ultimele_rezultate.html")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
ALTE_BUNURI = "Alte bunuri"


# ================================================================ utilitare
def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        print(line)
    except Exception:
        pass
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def norm(text):
    """litere mici, fara diacritice, spatii simple"""
    text = (text or "").replace("ş", "s").replace("ţ", "t").replace("Ş", "S").replace("Ţ", "T")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


_RX = {}


def _rx(part):
    """cuvant la INCEPUT de cuvant; cu '!' la final = cuvant intreg (ex. 'casa!')"""
    if part not in _RX:
        if part.endswith("!"):
            _RX[part] = re.compile(r"(?<![a-z0-9])" + re.escape(part[:-1]) + r"(?![a-z0-9])")
        else:
            _RX[part] = re.compile(r"(?<![a-z0-9])" + re.escape(part))
    return _RX[part]


def potriveste(text, cuvinte):
    """intoarce primul cuvant cheie ale carui cuvinte apar TOATE in text"""
    t = norm(text)
    for kw in cuvinte:
        parts = norm(kw).split()
        if parts and all(_rx(p).search(t) for p in parts):
            return kw
    return None


def exclus(text, excluse):
    t = norm(text)
    return any(norm(x) in t for x in excluse if x.strip())


def sesiune():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.5"})
    return s


def item(site, uid, titlu, link, cuvant, **extra):
    d = {"site": site, "id": f"{site}|{uid}", "titlu": " ".join((titlu or "").split()),
         "link": link, "cuvant": cuvant or ""}
    d.update({k: v for k, v in extra.items() if v})
    return d


def fmt_data(iso):
    try:
        return datetime.fromisoformat(iso[:19]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso or ""


def limita_data(cfg):
    return (datetime.now() - timedelta(days=cfg["zile_in_urma"])).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------- descarcare pagini
def descarca(s, url, js=False, timeout=60, gol_la_404=False):
    """intoarce HTML-ul paginii; js=True foloseste un browser (Playwright) pt. site-uri dinamice"""
    if js:
        return descarca_browser(url)
    try:
        r = s.get(url, timeout=timeout)
    except requests.exceptions.SSLError:
        # unele site-uri oficiale au certificat incomplet; citim doar pagini publice
        warnings.filterwarnings("ignore")
        r = s.get(url, timeout=timeout, verify=False)
    if gol_la_404 and r.status_code == 404:
        return ""
    if r.status_code in (403, 429, 503):
        try:
            return descarca_browser(url)        # unele site-uri blocheaza cererile simple
        except Exception:
            pass
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


_PW = {}


def descarca_browser(url):
    if "page" not in _PW:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("site dinamic - necesita Playwright (porneste 1_TEST.bat ca sa se instaleze)")
        _PW["pw"] = sync_playwright().start()
        _PW["browser"] = _PW["pw"].chromium.launch(headless=True)
        ctx = _PW["browser"].new_context(user_agent=UA, locale="ro-RO", ignore_https_errors=True)
        _PW["page"] = ctx.new_page()
    page = _PW["page"]
    page.goto(url, timeout=90000, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    return page.content()


def inchide_browser():
    try:
        if "browser" in _PW:
            _PW["browser"].close()
            _PW["pw"].stop()
    except Exception:
        pass


# ---------------------------------------------------------------- date calendaristice in text
LUNI = {"ian": 1, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "may": 5, "iun": 6, "jun": 6, "iul": 7, "jul": 7,
        "aug": 8, "sep": 9, "oct": 10, "noi": 11, "nov": 11, "dec": 12}
RX_DATE = [
    (re.compile(r"(?<!\d)(\d{1,2})[\./\-](\d{1,2})[\./\-](20\d\d)(?!\d)"), "dmy"),
    (re.compile(r"(?<!\d)(20\d\d)[\./\-](\d{1,2})[\./\-](\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<![a-z0-9])(\d{1,2})\s+([a-z]{3})[a-z]*\.?,?\s+(20\d\d)"), "dMy"),
    (re.compile(r"(?<![a-z0-9])([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(20\d\d)"), "Mdy"),
    (re.compile(r"(?<!\d)(\d{1,2})-([a-z]{3})\.?-(\d\d)(?!\d)"), "dMy2"),
]


def date_din_text(text):
    t = norm(text)
    rez = []
    for rx, fmt in RX_DATE:
        for m in rx.finditer(t):
            try:
                if fmt == "dmy":
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif fmt == "ymd":
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                elif fmt == "dMy2":
                    d, mo, y = int(m.group(1)), LUNI.get(m.group(2)), 2000 + int(m.group(3))
                elif fmt == "dMy":
                    d, mo, y = int(m.group(1)), LUNI.get(m.group(2)), int(m.group(3))
                else:
                    mo, d, y = LUNI.get(m.group(1)), int(m.group(2)), int(m.group(3))
                if mo:
                    rez.append(datetime(y, mo, d))
            except (ValueError, TypeError):
                pass
    return rez


# ================================================================ clasificare
# formule care apar in aproape orice anunt si ar duce la categorii gresite
RX_FORMULE = re.compile(r"(?<![a-z])(in stoc|pe stoc|stoc disponibil|numar (de )?inventar|nr\.? (de )?inventar|"
                        r"cu sediul( social)?|sediul social|obiecte de inventar de natura)(?![a-z])")


def curata_formule(text):
    return RX_FORMULE.sub(" ", norm(text))


class Cuvinte:
    def __init__(self, data):
        self.cuvinte_cheie = data.get("cuvinte_cheie", [])
        self.excluse = data.get("cuvinte_excluse", [])
        self.categorii = data.get("categorii", [])
        self.ordine = [c["nume"] for c in self.categorii] + [ALTE_BUNURI]

    def pentru_vanzari(self):
        """cuvintele folosite la cautarea pe site-urile de vanzari"""
        kw = list(self.cuvinte_cheie)
        for c in self.categorii:
            if c.get("folosit_la_cautare", True):
                kw += c.get("cuvinte", [])
        return list(dict.fromkeys(kw))

    def categorie(self, titlu, context=""):
        titlu, context = curata_formule(titlu), curata_formule(context)
        for text in (titlu, titlu + " " + context):
            for c in self.categorii:
                if potriveste(text, c.get("cuvinte", [])):
                    return c["nume"]
        return ALTE_BUNURI


# ================================================================ surse speciale
SEAP = "https://www.e-licitatie.ro"
SEAP_VIEW = {2: "c-notice", 17: "simplified-notice", 7: "pc-notice", 6: "dc-notice", 12: "rfq-invitation"}


def seap_proceduri(cfg, s, src, cuv):
    rez = []
    start = limita_data(cfg).strftime("%Y-%m-%dT00:00:00.000Z")
    hdr = {"Content-Type": "application/json;charset=UTF-8", "Referer": SEAP + "/pub/notices/c-notices"}
    for kw in cuv.cuvinte_cheie:
        page = 0
        while page < 10:
            body = {"sysNoticeTypeIds": [2, 6, 7, 17], "sortProperties": [], "pageSize": 50,
                    "hasUnansweredQuestions": False, "startPublicationDate": start,
                    "sysProcedureStateId": 2, "pageIndex": page, "contractName": kw}
            r = s.post(SEAP + "/api-pub/NoticeCommon/GetCNoticeList/", json=body, headers=hdr, timeout=60)
            r.raise_for_status()
            data = r.json()
            for it in data.get("items", []):
                tip = SEAP_VIEW.get(it.get("sysNoticeTypeId"), "c-notice")
                rez.append(item(
                    "SEAP - proceduri", it["cNoticeId"], it.get("contractTitle"),
                    f"{SEAP}/pub/notices/{tip}/v2/view/{it['cNoticeId']}", kw,
                    autoritate=it.get("contractingAuthorityNameAndFN"), valoare=it.get("estimatedValueExport"),
                    termen=it.get("tenderReceiptDeadlineExport"), publicat=fmt_data(it.get("noticeStateDate")),
                    cod=it.get("noticeNo"), cpv=it.get("cpvCodeAndName"), are_data=True))
            if (page + 1) * 50 >= data.get("total", 0):
                break
            page += 1
        time.sleep(0.5)
    return rez


def seap_publicitate(cfg, s, src, cuv):
    rez = []
    hdr = {"Content-Type": "application/json;charset=UTF-8", "Referer": SEAP + "/pub/adv-notices/list/0"}
    start = limita_data(cfg).strftime("%Y-%m-%dT00:00:00.000Z")
    for kw in cuv.cuvinte_cheie:
        body = {"pageSize": 100, "pageIndex": 0, "sortProperties": [], "contractObject": kw,
                "publicationDateStart": start}
        r = s.post(SEAP + "/api-pub/AdvNoticeCommon/GetAdvNoticeList/", json=body, headers=hdr, timeout=120)
        r.raise_for_status()
        for it in r.json().get("items", []):
            termen = it.get("tenderReceiptDeadline") or ""
            try:
                if termen and datetime.fromisoformat(termen[:19]) < datetime.now():
                    continue
            except Exception:
                pass
            pub = it.get("publicationDate") or ""
            rez.append(item(
                "SEAP - anunturi publicitate", it["advNoticeId"], it.get("contractObject"),
                f"{SEAP}/pub/notices/adv-notices/view/{it['advNoticeId']}", kw,
                autoritate=it.get("contractingAuthority"),
                valoare=(f"{it['estimatedValue']} RON" if it.get("estimatedValue") else None),
                termen=fmt_data(termen) if termen else None, publicat=fmt_data(pub) if pub else None,
                cod=it.get("noticeNo"), cpv=it.get("cpvCode"), are_data=True))
        time.sleep(0.5)
    return rez


def ted(cfg, s, src, cuv):
    """TED Europa - API oficial de cautare"""
    rez = []
    tari = " ".join(src.get("tari", ["ROU"]))
    de_la = limita_data(cfg).strftime("%Y%m%d")
    for kw in cuv.cuvinte_cheie:
        q = (f'publication-date>={de_la} AND buyer-country IN ({tari}) AND FT~("{kw}") '
             f'SORT BY publication-number DESC')
        body = {"query": q, "limit": 100, "page": 1, "scope": "ALL", "paginationMode": "PAGE_NUMBER",
                "fields": ["publication-number", "notice-title", "publication-date", "buyer-name",
                           "deadline-receipt-tender-date-lot"]}
        r = s.post("https://api.ted.europa.eu/v3/notices/search", json=body, timeout=60)
        r.raise_for_status()
        for n in r.json().get("notices", []):
            nr = n.get("publication-number")
            titlu = n.get("notice-title") or {}
            if isinstance(titlu, dict):
                titlu = titlu.get("ron") or titlu.get("eng") or next(iter(titlu.values()), "")
            cump = n.get("buyer-name") or {}
            if isinstance(cump, dict):
                v = cump.get("ron") or cump.get("eng") or next(iter(cump.values()), "")
                cump = v[0] if isinstance(v, list) and v else v
            if not potriveste(str(titlu), cuv.cuvinte_cheie):
                continue
            termen = n.get("deadline-receipt-tender-date-lot")
            if isinstance(termen, list):
                termen = termen[0] if termen else ""
            rez.append(item("TED Europa", nr, titlu, f"https://ted.europa.eu/ro/notice/-/detail/{nr}", kw,
                            autoritate=str(cump) if cump else None,
                            publicat=str(n.get("publication-date", ""))[:10],
                            termen=str(termen)[:10] if termen else None, cod=nr, are_data=True))
        time.sleep(1)
    return rez


def licitatia_ro(cfg, s, src, cuv):
    rez = []
    limita = limita_data(cfg)
    toate_kw = cuv.pentru_vanzari()
    for kw in toate_kw:
        for pagina in range(1, src.get("pagini", 2) + 1):
            url = f"https://www.licitatia.ro/search/index?expresie={quote(kw)}&tip_de_licitatie=4"
            if pagina > 1:
                url += f"&page={pagina}"
            soup = BeautifulSoup(descarca(s, url, gol_la_404=True), "html.parser")
            gasite, prea_vechi = 0, False
            for a in soup.find_all("a", href=True):
                href = a["href"].split("?")[0]
                m = re.search(r"-(\d{6,})(?:-[a-z]+)?\.html$", href)
                if not m or not a.find("h4"):
                    continue
                gasite += 1
                txt = a.get_text(" ", strip=True)
                pub = re.search(r"data publicarii\s*:\s*(\d{2}\.\d{2}\.\d{4})", txt, re.I)
                if pub and datetime.strptime(pub.group(1), "%d.%m.%Y") < limita:
                    prea_vechi = True
                    continue
                titlu = a.find("h4").get_text(" ", strip=True)
                kw_gasit = potriveste(titlu, toate_kw)   # cautarea site-ului e aproximativa
                if not kw_gasit:
                    continue
                val = re.search(r"valoare estimata\s*:\s*([^R]*RON|-)", txt, re.I)
                spans = [x.get_text(strip=True) for x in a.select("div.col.thirth span, div.col.third span")]
                rez.append(item("licitatia.ro - vanzari", m.group(1), titlu,
                                urljoin("https://www.licitatia.ro/", href), kw_gasit,
                                publicat=pub.group(1) if pub else None,
                                valoare=(val.group(1).strip() if val and val.group(1).strip() != "-" else None),
                                locatie=" ".join(spans) or None, are_data=bool(pub)))
            time.sleep(1)
            if prea_vechi or gasite < 20:
                break
    return rez


def ani_pentru_date(zile_luni):
    """zi/luna fara an, de la nou la vechi: cand data 'creste' am trecut in anul precedent"""
    azi = datetime.now()
    an, prev, rezultat = azi.year, (azi.month, azi.day), []
    for zl in zile_luni:
        if zl is None:
            rezultat.append(None)
            continue
        zi, luna = zl
        if (luna, zi) > prev:
            an -= 1
        prev = (luna, zi)
        try:
            rezultat.append(datetime(an, luna, zi))
        except ValueError:
            rezultat.append(None)
    return rezultat


def unpir_insolventa(cfg, s, src, cuv):
    rez = []
    limita = limita_data(cfg)
    for kw in cuv.pentru_vanzari():
        url = "https://www.licitatii-insolventa.ro/cauta/sKeyword," + quote(kw)
        soup = BeautifulSoup(descarca(s, url, gol_la_404=True), "html.parser")
        anunturi = []
        for bloc in soup.select("div.list-prod"):
            a = next((x for x in bloc.find_all("a", href=True) if re.search(r"_i\d+$", x["href"])), None)
            if not a:
                continue
            uid = re.search(r"_i(\d+)$", a["href"]).group(1)
            titlu = max((x.get_text(" ", strip=True) for x in bloc.find_all("a", href=re.compile(rf"_i{uid}$"))),
                        key=len, default="")
            dt = bloc.select_one("span.date")
            m = re.search(r"(\d{1,2})/(\d{1,2})", dt.get_text() if dt else "")
            anunturi.append((uid, titlu, urljoin(url, a["href"]), bloc,
                             (int(m.group(1)), int(m.group(2))) if m else None))
        for (uid, titlu, link, bloc, _), data_pub in zip(anunturi, ani_pentru_date([x[4] for x in anunturi])):
            if data_pub and data_pub < limita:
                continue
            desc_el = bloc.select_one("div.desc")
            descriere = desc_el.get_text(" ", strip=True) if desc_el else ""
            if not potriveste(titlu + " " + descriere, [kw]):
                continue
            pret = bloc.select_one("div.price")
            rez.append(item("licitatii-insolventa.ro (UNPIR)", uid, titlu, link, kw,
                            publicat=data_pub.strftime("%d.%m.%Y") if data_pub else None,
                            valoare=pret.get_text(" ", strip=True) if pret else None,
                            descriere=descriere[:250], are_data=bool(data_pub)))
        time.sleep(1)
    return rez


def citr(cfg, s, src, cuv):
    """pagina CITR 'Anunturi vanzare' contine doar anunturile active"""
    rez = []
    url = "https://sales.citr.ro/anunturi-licitatie/"
    soup = BeautifulSoup(descarca(s, url), "html.parser")
    for bloc in soup.select("div.anuntConatiner"):
        a = bloc.find("a", href=re.compile(r"AnuntLicitatie/\d+"))
        if not a:
            continue
        uid = re.search(r"AnuntLicitatie/(\d+)", a["href"]).group(1)
        text = bloc.get_text("\n", strip=True)
        linii = [l for l in text.split("\n") if l and not re.match(r"vezi detalii", l, re.I)]
        termen = re.search(r"termen limita de inscriere:\s*([^\n]+?)\s+pentru", text, re.I)
        rez.append(item("CITR vanzari", uid, linii[0] if linii else "", urljoin(url, a["href"]), "",
                        termen=termen.group(1) if termen else None, descriere=" ".join(linii[1:])[:250],
                        are_data=True))
    return rez


# ================================================================ sursa generica (orice site)
NAV_RX = re.compile(r"(^|[\s_-])(nav|navbar|navigation|menu|meniu|footer|header|breadcrumbs?|sidebar|"
                    r"social|cookie|cookies|share|login|topbar)([\s_-]|$)", re.I)
URL_IGNORAT = re.compile(
    r"/(category|categorie|tag|author|page|feed|wp-login|contact|despre|about|cookie|politica|privacy|"
    r"termeni|gdpr|login|cont|account|register|inregistrare|cart|cos)(/|$|\?)|mailto:|tel:|javascript:|"
    r"facebook\.com|linkedin\.com|twitter\.com|instagram\.com|youtube\.com|whatsapp|google\.com/maps", re.I)


def _in_navigatie(tag):
    parinti = list(tag.parents)
    if any(p.name == "article" for p in parinti):     # linkurile din articole sunt continut
        return False
    for p in parinti:
        if p.name in ("nav", "header", "footer"):
            return True
        ident = " ".join(p.get("class", [])) + " " + (p.get("id") or "")
        if ident.strip() and NAV_RX.search(ident):
            return True
    return False


def _norm_url(u):
    p = urlparse(u)
    q = "&".join(x for x in p.query.split("&") if x and not re.match(r"(utm_|fbclid|gclid|sid=|PHPSESSID)", x))
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", q, ""))


def _domeniu(u):
    h = urlparse(u).netloc.lower()
    return ".".join(h.split(".")[-2:])


RX_TITLU_GENERIC = re.compile(r"^(vezi|vedeti|afisati|citeste|mai multe|detalii|click|apasa|liciteaza|"
                              r"descarca|read more|more|view|details)\b")


def extrage_linkuri(html, url_pagina, src):
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(src["link_regex"], re.I) if src.get("link_regex") else None
    alte_domenii = src.get("alte_domenii", False)
    dom = _domeniu(url_pagina)
    candidati = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        u = _norm_url(urljoin(url_pagina, href))
        if not u.startswith("http") or URL_IGNORAT.search(u) or u.rstrip("/") == _norm_url(url_pagina).rstrip("/"):
            continue
        if rx:
            if not rx.search(u):
                continue
        else:
            if not alte_domenii and _domeniu(u) != dom:
                continue
            if urlparse(u).path.strip("/") == "" or _in_navigatie(a):
                continue
        text = a.get_text(" ", strip=True) or a.get("title", "") or \
            " ".join(img.get("alt", "") for img in a.find_all("img"))
        text = " ".join(text.split())
        if not rx and len(text) < 15:
            continue
        if u not in candidati:
            candidati[u] = {"a": a, "titlu": text}
        elif len(text) > len(candidati[u]["titlu"]):
            candidati[u]["titlu"] = text
    # contextul fiecarui link = cel mai mare bloc care nu contine alte anunturi
    toate = set(candidati)
    rezultat = []
    for u, c in candidati.items():
        bloc = c["a"]
        for _ in range(7):
            p = bloc.parent
            if p is None or p.name in ("body", "html"):
                break
            alte = {_norm_url(urljoin(url_pagina, x["href"])) for x in p.find_all("a", href=True)} & toate
            if len(alte) > 1:
                break
            bloc = p
        context = " ".join(bloc.get_text(" ", strip=True).split())[:700]
        titlu = c["titlu"] or context[:120]
        if RX_TITLU_GENERIC.match(norm(titlu)):
            h = bloc.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
            alt = " ".join(h.get_text(" ", strip=True).split()) if h else ""
            titlu = alt if len(alt) >= 6 and not RX_TITLU_GENERIC.match(norm(alt)) else context[:120]
        if len(titlu) < 4:
            continue
        rezultat.append((u, titlu, context))
    return rezultat


def extrage_tabel(html, url_pagina):
    soup = BeautifulSoup(html, "html.parser")
    rezultat = []
    for tr in soup.find_all("tr"):
        celule = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(celule) < 2:
            continue
        text = " | ".join(c for c in celule if c)
        if len(text) < 15:
            continue
        a = tr.find("a", href=True)
        link = urljoin(url_pagina, a["href"]) if a else url_pagina
        uid = hashlib.md5(norm(text).encode()).hexdigest()[:16]
        rezultat.append((uid, link, text))
    return rezultat


def pagina_generica(cfg, s, src, cuv):
    rez = []
    limita = limita_data(cfg)
    nume = src["nume"]
    filtru = src.get("filtru_cuvinte", False)
    kw_lista = cuv.pentru_vanzari()
    contine = [norm(x) for x in src.get("contine", [])]
    urls = src["url"] if isinstance(src["url"], list) else [src["url"]]
    if src.get("tip") == "cautare":   # url cu {kw}
        urls = [u.replace("{kw}", quote(k)) for u in urls for k in kw_lista]
    vazute = set()
    for url in urls:
        html = descarca(s, url, js=src.get("js", False))
        if src.get("mod") == "tabel":
            elemente = [(uid, link, text, text) for uid, link, text in extrage_tabel(html, url)]
        else:
            elemente = [(u, u, t, c) for u, t, c in extrage_linkuri(html, url, src)]
        for uid, link, titlu, context in elemente:
            if uid in vazute:
                continue
            vazute.add(uid)
            text_complet = titlu + " " + context + " " + link.replace("-", " ").replace("/", " ")
            if contine and not any(x in norm(text_complet) for x in contine):
                continue
            date = date_din_text(context + " " + link)
            if date and max(date) < limita:
                continue
            kw = potriveste(titlu + " " + context, kw_lista)
            if filtru and not kw:
                continue
            rez.append(item(nume, uid, titlu[:300], link, kw,
                            descriere=context[:250] if context != titlu else None,
                            publicat=max(date).strftime("%d.%m.%Y") if date else None,
                            are_data=bool(date), context=context))
        time.sleep(src.get("pauza", 1))
    return rez


SPECIALE = {
    "seap_proceduri": seap_proceduri,
    "seap_publicitate": seap_publicitate,
    "ted": ted,
    "licitatia_ro": licitatia_ro,
    "unpir_insolventa": unpir_insolventa,
    "citr": citr,
}


# ================================================================ raport + email
def html_raport(items, erori, titlu_raport, cuv, stare_surse=None):
    pe_cat = {}
    for it in items:
        pe_cat.setdefault(it["categorie"], []).append(it)
    rows = []
    for cat in cuv.ordine:
        lista = pe_cat.get(cat, [])
        if not lista:
            continue
        rows.append(f'<tr><td colspan="2" style="background:#2e7d32;color:#fff;padding:9px 10px;'
                    f'font-weight:bold;font-size:15px">{escape(cat)} ({len(lista)})</td></tr>')
        for it in sorted(lista, key=lambda x: (x.get("grup", ""), x["site"])):
            det = []
            for k, et in [("autoritate", "Autoritate"), ("valoare", "Valoare"), ("termen", "Termen"),
                          ("publicat", "Data"), ("locatie", "Locatie"), ("cod", "Cod"), ("cpv", "CPV"),
                          ("descriere", "Detalii")]:
                if it.get(k):
                    det.append(f"<b>{et}:</b> {escape(str(it[k]))}")
            rows.append(
                '<tr><td style="padding:8px 10px;border-bottom:1px solid #ddd">'
                f'<span style="background:#e8f5e9;color:#1b5e20;font-size:11px;padding:2px 6px;'
                f'border-radius:3px">{escape(it["site"])}</span><br>'
                f'<a href="{escape(it["link"])}" style="font-size:15px;color:#1b5e20;font-weight:bold">'
                f'{escape(it["titlu"] or "(fara titlu)")}</a><br>'
                f'<span style="color:#555;font-size:13px">{" &nbsp;|&nbsp; ".join(det)}</span></td>'
                f'<td style="padding:8px 10px;border-bottom:1px solid #ddd;color:#777;font-size:12px;'
                f'white-space:nowrap;vertical-align:top">{escape(it.get("cuvant") or "")}</td></tr>')
    err = ""
    if erori:
        err = ('<p style="color:#b71c1c"><b>Surse cu probleme la aceasta rulare:</b><br>'
               + "<br>".join(escape(e) for e in erori) + "</p>")
    stare = ""
    if stare_surse:
        r = "".join(
            f'<tr><td style="padding:3px 8px">{escape(n)}</td><td style="padding:3px 8px">{escape(g)}</td>'
            f'<td style="padding:3px 8px;text-align:right">{c}</td>'
            f'<td style="padding:3px 8px;color:{"#b71c1c" if st != "OK" else ("#e65100" if c == 0 else "#2e7d32")}">'
            f'{escape(st if st != "OK" else ("OK - 0 rezultate, de verificat" if c == 0 else "OK"))}</td></tr>'
            for n, g, c, st in stare_surse)
        stare = ('<h3 style="color:#2e7d32">Starea surselor</h3><table style="border-collapse:collapse;'
                 'font-size:13px"><tr><th align=left>Sursa</th><th align=left>Grup</th><th>Rezultate</th>'
                 f'<th align=left>Stare</th></tr>{r}</table>')
    return (f'<html><head><meta charset="utf-8"></head><body style="font-family:Arial,sans-serif">'
            f'<h2 style="color:#2e7d32">{escape(titlu_raport)}</h2>'
            f'<table style="border-collapse:collapse;width:100%;max-width:1000px">{"".join(rows)}</table>'
            f'{err}{stare}<p style="color:#999;font-size:12px">Generat automat {datetime.now():%d.%m.%Y %H:%M}'
            f' - Cautare licitatii GREEN KRAFT</p></body></html>')


# ================================================================ judet
JUDETE = ["Alba", "Arad", "Arges", "Bacau", "Bihor", "Bistrita-Nasaud", "Botosani", "Braila", "Brasov",
          "Buzau", "Calarasi", "Caras-Severin", "Cluj", "Constanta", "Covasna", "Dambovita", "Dolj", "Galati",
          "Giurgiu", "Gorj", "Harghita", "Hunedoara", "Ialomita", "Iasi", "Ilfov", "Maramures", "Mehedinti",
          "Mures", "Neamt", "Olt", "Prahova", "Salaj", "Satu Mare", "Sibiu", "Suceava", "Teleorman", "Timis",
          "Tulcea", "Valcea", "Vaslui", "Vrancea", "Bucuresti"]
ORASE = {
    "alba iulia": "Alba", "pitesti": "Arges", "campulung": "Arges", "onesti": "Bacau", "oradea": "Bihor",
    "bistrita": "Bistrita-Nasaud", "resita": "Caras-Severin", "cluj-napoca": "Cluj", "cluj napoca": "Cluj",
    "turda": "Cluj", "mangalia": "Constanta", "medgidia": "Constanta", "navodari": "Constanta",
    "sfantu gheorghe": "Covasna", "targoviste": "Dambovita", "craiova": "Dolj", "targu jiu": "Gorj",
    "miercurea ciuc": "Harghita", "deva": "Hunedoara", "petrosani": "Hunedoara", "hunedoara": "Hunedoara",
    "slobozia": "Ialomita", "baia mare": "Maramures", "drobeta": "Mehedinti", "turnu severin": "Mehedinti",
    "targu mures": "Mures", "piatra neamt": "Neamt", "slatina": "Olt", "ploiesti": "Prahova",
    "campina": "Prahova", "zalau": "Salaj", "alexandria": "Teleorman", "zimnicea": "Teleorman",
    "turnu magurele": "Teleorman", "timisoara": "Timis", "lugoj": "Timis", "ramnicu valcea": "Valcea",
    "barlad": "Vaslui", "focsani": "Vrancea", "ramnicu sarat": "Buzau", "otopeni": "Ilfov",
    "voluntari": "Ilfov", "afumati": "Ilfov", "popesti-leordeni": "Ilfov", "chiajna": "Ilfov",
    "bragadiru": "Ilfov", "pantelimon": "Ilfov", "buftea": "Ilfov", "stefanestii de jos": "Ilfov",
    "gaesti": "Dambovita", "tecuci": "Galati", "pascani": "Iasi", "falticeni": "Suceava",
    "sighisoara": "Mures", "medias": "Sibiu", "caracal": "Olt", "giurgiu": "Giurgiu",
}
_JUD_NORM = {norm(j): j for j in JUDETE}


def detecteaza_judet(*texte):
    t = norm(" ".join(x for x in texte if x))
    if not t:
        return None
    # 1) "jud. X" / "judetul X"
    for m in re.finditer(r"jud(?:etul|\.)?\s*([a-z\- ]{3,20})", t):
        frag = m.group(1)
        for jn, j in _JUD_NORM.items():
            if frag.startswith(jn.replace("-", " ")) or frag.startswith(jn):
                return j
    # 2) orase (fara Bucuresti - multi lichidatori au sediul acolo)
    for oras, j in ORASE.items():
        if re.search(r"(?<![a-z])" + re.escape(oras) + r"(?![a-z])", t):
            return j
    # 3) nume de judet
    for jn, j in _JUD_NORM.items():
        if j == "Bucuresti":
            continue
        variante = {jn, jn.replace("-", " ")}
        if any(re.search(r"(?<![a-z])" + re.escape(v) + r"(?![a-z])", t) for v in variante):
            return j
    # 4) Bucuresti
    if re.search(r"(?<![a-z])(bucuresti|sector(ul)? [1-6])(?![a-z])", t):
        return "Bucuresti"
    return None


# ================================================================ contacte (telefon / email)
RX_TEL = re.compile(r"(?<!\d)(?:\+?40[\s\.\-]?|0040[\s\.\-]?|0)([237]\d{1,2})[\s\.\-/]?(\d{3})[\s\.\-]?(\d{3,4})(?!\d)")
RX_MAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
FARA_DETALII = {"seap_proceduri", "seap_publicitate", "ted", "licitatia_ro", "brm"}


def contacte_din_text(text):
    tel, mail = [], []
    for m in RX_TEL.finditer(text or ""):
        nr = "0" + m.group(1) + m.group(2) + m.group(3)
        if len(nr) == 10 and nr not in tel:
            tel.append(nr)
    for m in RX_MAIL.finditer(text or ""):
        e = m.group(0).strip(".").lower()
        if re.search(r"\.(png|jpe?g|gif|webp|svg)$|sentry|example|wixpress|@2x", e):
            continue
        if e not in mail:
            mail.append(e)
    return tel[:2], mail[:2]


def text_principal(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "noscript"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):          # pastram adresele din linkuri mailto/tel
        if a["href"].startswith(("mailto:", "tel:")):
            a.append(" " + a["href"].split(":", 1)[1] + " ")
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return main.get_text(" ", strip=True)


def completeaza_detalii(s, it):
    """deschide pagina anuntului si cauta telefon, email, judet"""
    try:
        if it["link"].lower().endswith(".pdf"):
            return
        txt = text_principal(descarca(s, it["link"], timeout=25))
    except Exception:
        return
    tel, mail = contacte_din_text(txt)
    if tel and not it.get("telefon"):
        it["telefon"] = ", ".join(tel)
    if mail and not it.get("email"):
        it["email"] = ", ".join(mail)
    if not it.get("judet"):
        it["judet"] = detecteaza_judet(it["titlu"], txt[:4000])


# ================================================================ Supabase
def normalizeaza_url_supabase(url):
    """accepta orice forma copiata: https://REF.supabase.co, cu /rest/v1, sau link de dashboard"""
    u = url.strip().strip('"').strip("'").rstrip("/")
    m = re.search(r"https?://([a-z0-9]{20})\.supabase\.co", u)
    if m:
        return f"https://{m.group(1)}.supabase.co"
    m = re.search(r"/project/([a-z0-9]{20})", u) or re.fullmatch(r"([a-z0-9]{20})", u)
    if m:
        return f"https://{m.group(1)}.supabase.co"
    u = re.sub(r"/rest/v1/?$", "", u)
    return u if u.startswith("http") else "https://" + u


class Supabase:
    def __init__(self, url, cheie):
        baza = normalizeaza_url_supabase(url)
        self.url = baza + "/rest/v1/"
        gazda = urlparse(baza).netloc
        log(f"Supabase: {gazda[:4]}...{gazda[-16:]}")
        self.h = {"apikey": cheie, "Authorization": f"Bearer {cheie}", "Content-Type": "application/json"}

    def select(self, tabel, params):
        rez, start = [], 0
        while True:
            r = requests.get(self.url + tabel, params=params, timeout=60,
                             headers={**self.h, "Range": f"{start}-{start + 999}"})
            if r.status_code == 404:
                raise RuntimeError(
                    f"Tabelul '{tabel}' nu exista in proiectul Supabase din SUPABASE_URL. "
                    f"Ruleaza supabase.sql in SQL Editor-ul ACELUIASI proiect. Raspuns: {r.text[:200]}")
            if r.status_code == 401:
                raise RuntimeError("SUPABASE_SERVICE_KEY gresita (trebuie cheia service_role a aceluiasi proiect).")
            r.raise_for_status()
            bucata = r.json()
            rez += bucata
            if len(bucata) < 1000:
                return rez
            start += 1000

    def upsert(self, tabel, randuri):
        for i in range(0, len(randuri), 200):
            r = requests.post(self.url + tabel, json=randuri[i:i + 200], timeout=120,
                              headers={**self.h, "Prefer": "resolution=merge-duplicates,return=minimal"})
            if r.status_code >= 300:
                raise RuntimeError(f"Supabase {tabel}: {r.status_code} {r.text[:300]}")

    def update_in(self, tabel, ids, valori):
        for i in range(0, len(ids), 150):
            r = requests.patch(self.url + tabel, params={"id": "in.(" + ",".join(ids[i:i + 150]) + ")"},
                               json=valori, timeout=120, headers={**self.h, "Prefer": "return=minimal"})
            if r.status_code >= 300:
                raise RuntimeError(f"Supabase {tabel}: {r.status_code} {r.text[:300]}")


def _sterge_in(sb, tabel, ids):
    for i in range(0, len(ids), 150):
        r = requests.delete(sb.url + tabel, params={"id": "in.(" + ",".join(ids[i:i + 150]) + ")"},
                            timeout=120, headers={**sb.h, "Prefer": "return=minimal"})
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase {tabel}: {r.status_code} {r.text[:300]}")


def intretinere(sb, cuv, cfg):
    """reclasifica toate anunturile dupa cuvintele actuale si sterge anunturile vechi (nu si pe cele urmarite)"""
    randuri = sb.select("lic_anunturi", {"select": "id,titlu,descriere,categorie"})
    fav = {r["anunt_id"] for r in sb.select("lic_favorite", {"select": "anunt_id"})}
    limita = limita_data(cfg)
    de_sters, schimbari = [], {}
    for r in randuri:
        text = (r.get("titlu") or "") + " " + (r.get("descriere") or "")
        d = date_din_text(text)
        if d and max(d) < limita and r["id"] not in fav:
            de_sters.append(r["id"])
            continue
        c = cuv.categorie(r.get("titlu") or "", r.get("descriere") or "")
        if c != r.get("categorie"):
            schimbari.setdefault(c, []).append(r["id"])
    # titluri generice salvate inainte ("Vedeti detalii") -> inceputul descrierii
    for r in randuri:
        if r["id"] in de_sters or not r.get("descriere"):
            continue
        if RX_TITLU_GENERIC.match(norm(r.get("titlu") or "")):
            titlu_nou = re.sub(r"\s*(vezi|vedeti|vedeți|detalii|liciteaza)\b.*$", "", r["descriere"], flags=re.I)[:150].strip()
            if len(titlu_nou) >= 6:
                sb.update_in("lic_anunturi", [r["id"]], {"titlu": titlu_nou})
    if de_sters:
        _sterge_in(sb, "lic_anunturi", de_sters)
    for c, ids in schimbari.items():
        sb.update_in("lic_anunturi", ids, {"categorie": c})
    log(f"Intretinere: {sum(len(v) for v in schimbari.values())} reclasificate, {len(de_sters)} vechi sterse")


def cod_unic(cheie):
    return hashlib.md5(cheie.encode("utf-8")).hexdigest()[:24]


def data_iso(text):
    d = date_din_text(text or "")
    return max(d).strftime("%Y-%m-%d") if d else None


# ================================================================ email
def trimite_email(destinatari, subiect, html):
    server = os.environ.get("SMTP_SERVER") or "mail.greenkraft.ro"
    port = int(os.environ.get("SMTP_PORT") or "465")
    user = os.environ.get("SMTP_USER") or "office@greenkraft.ro"
    parola = os.environ.get("SMTP_PAROLA") or ""
    if not parola:
        log("SMTP_PAROLA lipseste - emailul nu a fost trimis")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subiect
    msg["From"] = user
    msg["To"] = ", ".join(destinatari)
    msg.attach(MIMEText(html, "html", "utf-8"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(server, port, context=ctx, timeout=60) as srv:
            srv.login(user, parola)
            srv.sendmail(user, destinatari, msg.as_string())
    else:
        with smtplib.SMTP(server, port, timeout=60) as srv:
            srv.starttls(context=ctx)
            srv.login(user, parola)
            srv.sendmail(user, destinatari, msg.as_string())


# ================================================================ main
def main():
    test = "--test" in sys.argv
    doar = None
    if "--sursa" in sys.argv:
        doar = set(sys.argv[sys.argv.index("--sursa") + 1].split(","))

    with open(F_SURSE, encoding="utf-8") as f:
        surse_cfg = json.load(f)
    with open(F_CUVINTE, encoding="utf-8") as f:
        cuv_data = json.load(f)

    sb = None
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        sb = Supabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    elif not test:
        raise RuntimeError("Lipsesc SUPABASE_URL / SUPABASE_SERVICE_KEY (sau ruleaza cu --test)")

    # setarile din aplicatia web
    setari = {}
    if sb:
        rows = sb.select("lic_setari", {"select": "date", "id": "eq.1"})
        setari = rows[0]["date"] if rows else {}
    cuv_data["cuvinte_cheie"] = list(dict.fromkeys(cuv_data.get("cuvinte_cheie", []) + setari.get("cuvinte_extra", [])))
    cuv_data["cuvinte_excluse"] = cuv_data.get("cuvinte_excluse", []) + setari.get("cuvinte_excluse", [])
    cuv = Cuvinte(cuv_data)
    cfg = {"zile_in_urma": int(setari.get("zile_in_urma") or surse_cfg.get("zile_in_urma", 60))}

    # surse adaugate / oprite din aplicatia web
    oprite = set(setari.get("surse_oprite") or [])
    if sb:
        try:
            for x in sb.select("lic_surse_extra", {"select": "*", "activ": "eq.true"}):
                urls = [u.strip() for u in (x.get("url") or "").splitlines() if u.strip().startswith("http")]
                if urls:
                    surse_cfg["surse"].append({
                        "id": f"extra_{x['id']}", "grup": x.get("grup") or "7. Adaugate din aplicatie",
                        "nume": x.get("nume") or urls[0], "url": urls, "activ": True,
                        "filtru_cuvinte": bool(x.get("filtru_cuvinte")), "js": bool(x.get("js"))})
        except Exception as ex:
            log(f"Nu am putut citi sursele adaugate din aplicatie: {ex}")

    s = sesiune()
    toate, erori, stare = [], [], []
    for src in surse_cfg["surse"]:
        if src["id"] in oprite and doar is None:
            continue
        if doar is not None:
            if src["id"] not in doar:
                continue
        elif not src.get("activ", True):
            continue
        functie = SPECIALE.get(src.get("tip"), pagina_generica)
        try:
            r = functie(cfg, s, src, cuv)
            for it in r:
                it["grup"] = src.get("grup", "")
                it["sursa_id"] = src["id"]
            log(f"{src['id']}: {len(r)} rezultate")
            toate.extend(r)
            stare.append((src, len(r), "OK"))
        except Exception as ex:
            msg = str(ex)[:200]
            erori.append(f"{src['nume']}: {msg}")
            stare.append((src, 0, "EROARE: " + msg))
            log(f"EROARE {src['id']}: {ex}")
    inchide_browser()

    # deduplicare, excluderi, clasificare, judet, contacte din context
    unice = {}
    for it in toate:
        if exclus(it["titlu"], cuv.excluse):
            continue
        cod = cod_unic(it["id"])
        if cod in unice:
            continue
        ctx = (it.get("context") or "") + " " + (it.get("descriere") or "")
        it["cod"] = cod
        it["desc_db"] = ((it.get("context") or it.get("descriere") or "")[:600]) or None
        if it["desc_db"] == it["titlu"]:
            it["desc_db"] = None
        it["categorie"] = cuv.categorie(it["titlu"], it["desc_db"] or "")
        it["judet"] = detecteaza_judet(it["titlu"], it.get("locatie"), it.get("autoritate"), ctx)
        tel, mail = contacte_din_text(ctx)
        if tel:
            it["telefon"] = ", ".join(tel)
        if mail:
            it["email"] = ", ".join(mail)
        unice[cod] = it
    rezultate = list(unice.values())

    existente, initiate = set(), set()
    if sb:
        existente = {r["id"] for r in sb.select("lic_anunturi", {"select": "id"})}
        initiate = {r["sursa_id"] for r in sb.select("lic_surse_stare", {"select": "sursa_id", "initiata": "eq.true"})}
    noi = [it for it in rezultate if it["cod"] not in existente]
    vechi_ids = [it["cod"] for it in rezultate if it["cod"] in existente]

    # pagina fiecarui anunt NOU -> telefon / email / judet (max 120 pe rulare)
    deschise = 0
    for it in noi:
        if deschise >= 120:
            break
        if it["sursa_id"] in FARA_DETALII or (it.get("telefon") and it.get("email") and it.get("judet")):
            continue
        completeaza_detalii(s, it)
        deschise += 1
        time.sleep(0.5)
    log(f"Total: {len(rezultate)} gasite, {len(noi)} noi (detalii citite: {deschise})")

    if test:
        os.makedirs(os.path.dirname(RAPORT), exist_ok=True)
        for it in rezultate:
            extra = [x for x in (it.get("judet"), it.get("telefon"), it.get("email")) if x]
            if extra:
                it["locatie"] = " | ".join(extra)
        stare_txt = [(src["nume"], src.get("grup", ""), n, st) for src, n, st in stare]
        with open(RAPORT, "w", encoding="utf-8") as f:
            f.write(html_raport(rezultate, erori, f"TEST - {len(rezultate)} licitatii", cuv, stare_txt))
        try:
            webbrowser.open("file:///" + RAPORT.replace("\\", "/"))
        except Exception:
            pass
        log(f"Mod test: raport in {RAPORT}")
        return

    acum = datetime.utcnow().isoformat() + "Z"
    randuri = []
    for it in noi:
        randuri.append({
            "id": it["cod"], "cheie": it["id"][:500], "sursa_id": it["sursa_id"], "sursa": it["site"],
            "grup": it.get("grup"), "categorie": it["categorie"], "titlu": it["titlu"][:500],
            "link": it["link"], "descriere": it.get("desc_db"),
            "autoritate": it.get("autoritate"), "valoare": it.get("valoare"), "termen": it.get("termen"),
            "publicat": it.get("publicat"), "data_publicare": data_iso(it.get("publicat")),
            "judet": it.get("judet"), "telefon": it.get("telefon"), "email": it.get("email"),
            "cuvant": it.get("cuvant") or None, "prima_aparitie": acum, "ultima_aparitie": acum,
        })
    sb.upsert("lic_anunturi", randuri)
    if vechi_ids:
        sb.update_in("lic_anunturi", vechi_ids, {"ultima_aparitie": acum})
    sb.upsert("lic_surse_stare", [
        {"sursa_id": src["id"], "nume": src["nume"], "grup": src.get("grup", ""),
         "initiata": (src["id"] in initiate) or st == "OK", "ultima_rulare": acum, "rezultate": n, "stare": st}
        for src, n, st in stare])
    log(f"Salvat in Supabase: {len(randuri)} noi, {len(vechi_ids)} actualizate")
    try:
        intretinere(sb, cuv, cfg)
    except Exception as ex:
        log(f"EROARE intretinere: {ex}")

    # email: doar anunturi noi, de la surse deja initiate (sau cu data), filtrate dupa setari
    if not setari.get("email_activ", True):
        return
    if not existente:
        log("Prima rulare: anunturile au fost salvate in aplicatie, fara email.")
        return
    de_trimis = [it for it in noi if it["sursa_id"] in initiate or it.get("are_data")]
    judete = set(setari.get("judete_notificari") or [])
    if judete:
        de_trimis = [it for it in de_trimis if not it.get("judet") or it["judet"] in judete]
    categorii = set(setari.get("categorii_notificari") or [])
    if categorii:
        de_trimis = [it for it in de_trimis if it["categorie"] in categorii]
    dest = setari.get("email_destinatari") or ["office@greenkraft.ro"]
    if de_trimis and dest:
        for it in de_trimis:
            extra = [x for x in (it.get("judet"), it.get("telefon"), it.get("email")) if x]
            if extra:
                it["locatie"] = " | ".join(extra)
        pe_cat = {}
        for it in de_trimis:
            pe_cat[it["categorie"]] = pe_cat.get(it["categorie"], 0) + 1
        rezumat = ", ".join(f"{c}: {pe_cat[c]}" for c in cuv.ordine if c in pe_cat)
        total = len(de_trimis)
        ordine = {c: i for i, c in enumerate(cuv.ordine)}
        de_trimis = sorted(de_trimis, key=lambda x: ordine.get(x["categorie"], 99))[:300]
        titlu_email = f"{total} licitatii noi" + (f" (primele 300 aici, restul in aplicatie)" if total > 300 else "")
        html = html_raport(de_trimis, erori, titlu_email, cuv)
        app = os.environ.get("APP_URL")
        if app:
            html = html.replace("</h2>", f'</h2><p><a href="{escape(app)}" style="font-size:15px">'
                                          f'Deschide aplicatia de licitatii</a></p>', 1)
        try:
            trimite_email(dest, f"[Licitatii] {total} noi ({rezumat})"[:250], html)
            log(f"Email trimis catre {', '.join(dest)}")
        except Exception as ex:
            log(f"EROARE EMAIL (anunturile sunt salvate in aplicatie): {ex}. "
                f"Verifica secretele SMTP_PAROLA / SMTP_USER / SMTP_SERVER in GitHub.")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        log(f"EROARE GENERALA: {ex}\n{traceback.format_exc()}")
        inchide_browser()
        sys.exit(1)
