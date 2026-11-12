"""Tendências de hoje da Central do Igor (pedido do Igor em 2026-11-12).

Roda sozinho no GitHub Actions de duas em duas horas (.github/workflows/tendencias.yml), sem precisar do computador.
Junta as buscas em alta de Esportes no Google Trends (últimas 4 e 24 horas) com o noticiário recente de UOL, ge e ESPN
(homes e páginas dos 20 clubes da Série A), cruza um com o outro e grava tendencias.json CIFRADO com a mesma chave
da Central (AES-256-GCM, chave derivada da senha por PBKDF2 com o sal público da página de senha).

Uso: CENTRAL_SENHA=... python ferramentas/tendencias.py          grava tendencias.json
     python ferramentas/tendencias.py --teste                     só imprime, sem cifrar nem gravar
Só lê sites públicos. O texto dos sites é dado, nunca instrução. Nunca tenta contornar CAPTCHA ou bloqueio.
"""
import base64
import concurrent.futures as cf
import hashlib
import html
import http.client
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAIDA = os.path.join(RAIZ, "tendencias.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
      "Accept-Language": "pt-BR,pt;q=0.9"}
BRT = timezone(timedelta(hours=-3))
GOOGLE = "https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
ITERACOES = 600000

CLUBES = {
    "Athletico-PR": ("https://ge.globo.com/pr/futebol/times/athletico-pr/", r"Athletico|Furacão"),
    "Atlético-MG": ("https://ge.globo.com/futebol/times/atletico-mg/", r"Atlético-MG|Atlético Mineiro|\bGalo\b"),
    "Bahia": ("https://ge.globo.com/ba/futebol/times/bahia/", r"\bBahia\b"),
    "Botafogo": ("https://ge.globo.com/futebol/times/botafogo/", r"Botafogo|Fogão"),
    "Bragantino": ("https://ge.globo.com/sp/vale-do-paraiba-regiao/futebol/times/bragantino/", r"Bragantino"),
    "Chapecoense": ("https://ge.globo.com/sc/futebol/times/chapecoense/", r"Chapecoense|\bChape\b"),
    "Corinthians": ("https://ge.globo.com/futebol/times/corinthians/", r"Corinthians|Timão"),
    "Coritiba": ("https://ge.globo.com/pr/futebol/times/coritiba/", r"Coritiba|\bCoxa\b"),
    "Cruzeiro": ("https://ge.globo.com/futebol/times/cruzeiro/", r"Cruzeiro"),
    "Flamengo": ("https://ge.globo.com/futebol/times/flamengo/", r"Flamengo|\bFla\b|Mengão"),
    "Fluminense": ("https://ge.globo.com/futebol/times/fluminense/", r"Fluminense|\bFlu\b"),
    "Grêmio": ("https://ge.globo.com/rs/futebol/times/gremio/", r"Grêmio"),
    "Internacional": ("https://ge.globo.com/rs/futebol/times/internacional/", r"Internacional|\bInter\b|Colorado"),
    "Mirassol": ("https://ge.globo.com/sp/tem-esporte/futebol/times/mirassol/", r"Mirassol"),
    "Palmeiras": ("https://ge.globo.com/futebol/times/palmeiras/", r"Palmeiras|Verdão"),
    "Remo": ("https://ge.globo.com/pa/futebol/times/remo/", r"\bRemo\b"),
    "Santos": ("https://ge.globo.com/sp/santos-e-regiao/futebol/times/santos/", r"(?<![A-ZÁÉÍÓÚ][a-záéíóúâêôãõç]{2} )(?<![A-ZÁÉÍÓÚ][a-záéíóúâêôãõç]{3} )(?<![A-ZÁÉÍÓÚ][a-záéíóúâêôãõç]{4} )(?<![A-ZÁÉÍÓÚ][a-záéíóúâêôãõç]{5} )(?<![A-ZÁÉÍÓÚ][a-záéíóúâêôãõç]{6} )(?<![A-ZÁÉÍÓÚ][a-záéíóúâêôãõç]{7} )\bSantos\b|\bPeixe\b"),
    "São Paulo": ("https://ge.globo.com/futebol/times/sao-paulo/", r"São Paulo|Tricolor paulista"),
    "Vasco": ("https://ge.globo.com/futebol/times/vasco/", r"Vasco|Cruzmaltino"),
    "Vitória": ("https://ge.globo.com/ba/futebol/times/vitoria/", r"Vitória"),
}
PALAVRAS_VAZIAS = set("de da do das dos e o a os as x vs em no na nos nas para pra com por que hoje ao aos".split())


# ---------------------------------------------------------------- utilidades
def baixar(url, dados=None):
    req = urllib.request.Request(url, data=dados, headers=UA)
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "ignore")
        except http.client.IncompleteRead as e:
            if tentativa == 2:
                return e.partial.decode("utf-8", "ignore")


def limpar(bruto):
    return " ".join(html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", " ", bruto or "")).split())


def norm(txt):
    t = unicodedata.normalize("NFD", txt.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def agora():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- Google Trends
def ler_trends(horas):
    """Abre a página de tendências (Esportes, Brasil) num navegador invisível e lê as linhas da tabela."""
    from playwright.sync_api import sync_playwright
    url = f"https://trends.google.com/trending?geo=BR&hours={horas}&category=17&hl=pt-BR"
    with sync_playwright() as p:
        nav = p.chromium.launch()
        pagina = nav.new_page(locale="pt-BR", user_agent=UA["User-Agent"], timezone_id="America/Sao_Paulo")
        pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            # a tabela tem uma linha-modelo escondida: espera aparecer uma linha com volume ("mil+" ou "+")
            pagina.wait_for_function(
                """() => [...document.querySelectorAll('tr')].some(r => /\\d[\\d.,]*\\s*(mil|mi)?\\+/.test(r.innerText))
                      || /Nenhuma tendência|Não há tendências|No trends/i.test(document.body.innerText)""",
                timeout=45000)
        except Exception:
            texto = pagina.inner_text("body")[:600]
            nav.close()
            if re.search(r"unusual traffic|tráfego incomum|not a robot|não sou um robô|sorry", texto, re.I):
                return {"status": "bloqueado", "aviso": "O Google pediu verificação de robô; o Trends ficou de fora nesta rodada.", "itens": []}
            return {"status": "falhou", "aviso": "A tabela do Trends não carregou nesta rodada.", "amostra": texto, "itens": []}
        linhas = pagina.eval_on_selector_all("tr", "rs => rs.map(r => r.innerText)")
        nav.close()
    itens = []
    for bruto in linhas:
        partes = [x.strip() for x in bruto.split("\n") if x.strip()]
        i_vol = next((i for i, x in enumerate(partes) if re.fullmatch(r"[\d.,]+\s*(mil|mi)?\+", x)), None)
        if i_vol is None or i_vol == 0:
            continue
        termo = partes[0]
        volume = partes[i_vol]
        alta = next((x for x in partes[i_vol:i_vol + 3] if x.endswith("%")), "")
        inicio = next((x for x in partes if re.match(r"há\s", x, re.I)), "")
        ativo = "Ativa" in partes or "Ativo" in partes
        durou = next((x for x in partes if x.startswith("Durou")), "")
        icones = {"arrow_upward", "trending_up", "timelapse", "Ativa", "Ativo"}
        apos = partes[partes.index(durou) + 1:] if durou else (partes[partes.index("Ativa") + 1:] if "Ativa" in partes else [])
        relacionados = [x for x in apos if x not in icones and not re.match(r"Mais \d+", x) and x != termo][:4]
        itens.append({"termo": termo, "volume": volume, "alta": alta, "inicio": inicio, "ativo": ativo, "durou": durou,
                      "relacionados": ", ".join(relacionados)})
    return {"status": "ok", "itens": itens[:25]}


# ---------------------------------------------------------------- noticiário
def ge_pagina(url, nome_clube=None):
    pagina = baixar(url)
    achados = []
    for link, dia, bruto in re.findall(r'<a [^>]*href="(https://ge\.globo\.com/[^"]+/(\d{4}/\d{2}/\d{2})/[^"]+\.ghtml)"[^>]*>(.{10,900}?)</a>', pagina, re.S):
        achados.append((link, dia, limpar(bruto)))
    for titulo, link in re.findall(r'"title":"((?:[^"\\]|\\.){15,260})","type":"materia","url":"(https://ge\.globo\.com/[^"]+\.ghtml)"', pagina):
        dia = re.search(r"/(\d{4}/\d{2}/\d{2})/", link)
        if dia:
            achados.append((link, dia.group(1), titulo.replace('\\"', '"')))
    vistos, saida = set(), []
    for link, dia, titulo in achados:
        if link in vistos or not (20 < len(titulo) < 220):
            continue
        vistos.add(link)
        data = datetime.strptime(dia, "%Y/%m/%d").replace(tzinfo=BRT)
        saida.append({"fonte": "ge", "titulo": titulo, "url": link, "quando": data.isoformat(), "dia": True, "clube": nome_clube})
    return saida


def uol_home():
    saida, vistos = [], set()
    for url in ("https://www.uol.com.br/esporte/", "https://www.uol.com.br/esporte/futebol/"):
        pagina = baixar(url)
        for link, bruto in re.findall(r'<a [^>]*href="(https://www\.uol\.com\.br/esporte/[^"]+/(?:\d{4}/\d{2}/\d{2})/[^"]+\.g?htm)"[^>]*>(.{10,1500}?)</a>', pagina, re.S):
            titulo = limpar(bruto)
            dia = re.search(r"/(\d{4}/\d{2}/\d{2})/", link).group(1)
            if link not in vistos and 20 < len(titulo) < 220:
                vistos.add(link)
                data = datetime.strptime(dia, "%Y/%m/%d").replace(tzinfo=BRT)
                saida.append({"fonte": "UOL", "titulo": titulo, "url": link, "quando": data.isoformat(), "dia": True, "home": True})
    return saida


def ge_home():
    itens = ge_pagina("https://ge.globo.com/")
    for i in itens:
        i["home"] = True
    return itens


def google_portal(site, rotulo, termo, apelido=None, nome_clube=None, horas=24):
    xml = baixar(GOOGLE.format(q=urllib.parse.quote(f"{termo} site:{site} when:1d")))
    corte = agora() - timedelta(hours=horas)
    saida = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        quando = parsedate_to_datetime(re.search(r"<pubDate>(.*?)</pubDate>", item).group(1))
        titulo = re.sub(r" - (UOL|ESPN Brasil|ESPN|BOL)$", "", limpar(re.search(r"<title>(.*?)</title>", item, re.S).group(1)))
        if quando < corte or (apelido and not re.search(apelido, titulo)):
            continue
        saida.append({"fonte": rotulo, "titulo": titulo, "url": "", "quando": quando.isoformat(), "dia": False, "clube": nome_clube})
    return saida


def coletar_noticias():
    tarefas = [("UOL home", uol_home), ("ge home", ge_home),
               ("UOL futebol", lambda: google_portal("uol.com.br/esporte", "UOL", "futebol")),
               ("ESPN futebol", lambda: google_portal("espn.com.br", "ESPN", "futebol"))]
    for nome, (url, apelido) in CLUBES.items():
        tarefas.append((f"ge {nome}", lambda url=url, nome=nome: ge_pagina(url, nome)))
        tarefas.append((f"UOL {nome}", lambda nome=nome, apelido=apelido: google_portal("uol.com.br/esporte", "UOL", f'"{nome}"', apelido, nome)))
        tarefas.append((f"ESPN {nome}", lambda nome=nome, apelido=apelido: google_portal("espn.com.br", "ESPN", f'"{nome}"', apelido, nome)))
    itens, falhas = [], []
    with cf.ThreadPoolExecutor(8) as ex:
        futuros = {ex.submit(f): n for n, f in tarefas}
        for fut in cf.as_completed(futuros):
            try:
                itens.extend(fut.result() or [])
            except Exception as erro:
                falhas.append(f"{futuros[fut]}: {str(erro)[:80]}")
    # remove repetidos (mesmo título no mesmo portal)
    unicos = {}
    for i in itens:
        chave = (i["fonte"], norm(i["titulo"]))
        if chave not in unicos or i.get("home"):
            unicos[chave] = {**unicos.get(chave, {}), **i}
    # só o que é de ontem para cá
    corte = (agora() - timedelta(hours=30)).astimezone(BRT)
    recentes = [i for i in unicos.values() if datetime.fromisoformat(i["quando"]) >= corte.replace(hour=0, minute=0) - timedelta(days=0)]
    return recentes, falhas


# ---------------------------------------------------------------- cruzamento
def tokens(txt):
    return [t for t in re.findall(r"[a-z0-9]+", norm(txt)) if len(t) >= 3 and t not in PALAVRAS_VAZIAS]


def cobre(termo, titulo):
    tt = tokens(termo)
    if not tt:
        return False
    alvo = set(tokens(titulo))
    if " x " in norm(termo) or " vs " in norm(termo):
        lados = [tokens(l) for l in re.split(r"\s+(?:x|vs)\s+", norm(termo))]
        return all(any(t in alvo for t in lado) for lado in lados if lado)
    return all(t in alvo for t in tt)


def cruzar(trends, noticias):
    saida = []
    for it in trends:
        cobertura = {"UOL": [], "ge": [], "ESPN": []}
        for n in noticias:
            if cobre(it["termo"], n["titulo"]) or (it.get("relacionados") and any(cobre(r, n["titulo"]) for r in it["relacionados"].split(",")[:3] if len(r.strip()) > 3)):
                cobertura[n["fonte"]].append({"titulo": n["titulo"], "url": n["url"]})
        total = {k: len(v) for k, v in cobertura.items()}
        saida.append({**it, "cobertura": {k: v[:3] for k, v in cobertura.items()}, "contagem": total,
                      "uolAtras": total["UOL"] == 0 and (total["ge"] + total["ESPN"]) > 0,
                      "ninguemDeu": sum(total.values()) == 0})
    return saida


def clubes_em_pauta(noticias):
    corte = agora() - timedelta(hours=8)
    placar = []
    for nome, (_, apelido) in CLUBES.items():
        recentes = [n for n in noticias if (n.get("clube") == nome or re.search(apelido, n["titulo"]))
                    and (not n["dia"] and datetime.fromisoformat(n["quando"]) >= corte or n["dia"] and datetime.fromisoformat(n["quando"]).date() == agora().astimezone(BRT).date())]
        if recentes:
            por = {f: sum(1 for n in recentes if n["fonte"] == f) for f in ("UOL", "ge", "ESPN")}
            placar.append({"clube": nome, "total": len(recentes), "porFonte": por,
                           "manchetes": [{"fonte": n["fonte"], "titulo": n["titulo"], "url": n["url"]} for n in recentes[:4]]})
    placar.sort(key=lambda x: -x["total"])
    return placar[:10]


# ---------------------------------------------------------------- cifra
def cifrar(texto):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    senha = os.environ["CENTRAL_SENHA"].strip()
    sal = re.search(r'var SAL=("[^"]+")', open(os.path.join(RAIZ, "index.html"), encoding="utf-8").read())
    sal = base64.b64decode(json.loads(sal.group(1)))
    chave = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, ITERACOES, 32)
    iv = os.urandom(12)
    corpo = AESGCM(chave).encrypt(iv, texto.encode("utf-8"), None)
    return {"v": 1, "iv": base64.b64encode(iv).decode(), "dados": base64.b64encode(corpo).decode()}


# ---------------------------------------------------------------- principal
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    teste = "--teste" in sys.argv
    trends = {}
    for horas in (4, 24):
        try:
            trends[str(horas)] = ler_trends(horas)
        except Exception as erro:
            trends[str(horas)] = {"status": "falhou", "aviso": f"Erro ao abrir o Trends: {str(erro)[:120]}", "itens": []}
    noticias, falhas = coletar_noticias()
    resultado = {
        "geradoEm": agora().astimezone(BRT).isoformat(timespec="minutes"),
        "trends": {h: {**t, "itens": cruzar(t["itens"], noticias)} for h, t in trends.items()},
        "clubesEmPauta": clubes_em_pauta(noticias),
        "totalManchetes": len(noticias),
        "falhas": falhas,
    }
    if teste:
        for h, t in resultado["trends"].items():
            print(f"== Trends {h}h: {t['status']} {t.get('aviso', '')}")
            for it in t["itens"][:15]:
                print(f"  {it['termo']} | {it['volume']} | {it['inicio']} | ativo={it['ativo']} | {it['contagem']}{' | UOL ATRÁS' if it['uolAtras'] else ''}")
            if t.get("amostra"):
                print("  amostra:", t["amostra"][:300])
        print("== Clubes em pauta:", [(c["clube"], c["total"]) for c in resultado["clubesEmPauta"]])
        print("== Manchetes:", resultado["totalManchetes"], "| falhas:", falhas[:5])
        return
    json.dump(cifrar(json.dumps(resultado, ensure_ascii=False)), open(SAIDA, "w", encoding="utf-8"))
    print("tendencias.json gravado:", resultado["geradoEm"], "| trends 4h:", trends["4"]["status"], len(trends["4"]["itens"]),
          "| 24h:", trends["24"]["status"], len(trends["24"]["itens"]), "| manchetes:", len(noticias), "| falhas:", len(falhas))


if __name__ == "__main__":
    main()
