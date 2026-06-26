# RPA ELAW — Código para Revisão do Programador

**Data:** 06/05/2026  
**Problema principal:** A busca por ID (`#Filters_idList`) retorna "Nenhum resultado" para a maioria dos IDs, mesmo eles existindo no sistema.

---

## Problema Central

O robô navega para `/processo/list?cache=false`, preenche o campo `#Filters_idList` com o ID numérico (ex: `1064381`), clica em `#buttonSubmit` via JavaScript e lê a tabela — mas a tabela sempre volta vazia, mesmo para IDs que existem confirmadamente no ELAW.

**Apenas ~2-3 IDs em 30 foram encontrados** (aparentemente aleatórios). Os restantes retornam "Nenhum registro localizado".

### O que já foi tentado (sem sucesso):

1. `page.fill()` → detectado como bot, campo limpo pelo ELAW
2. `page.type(delay=40)` com eventos `input`/`change`/`keyup` → campo fica preenchido mas busca retorna vazio
3. Setter nativo JS (`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set`) → mesmos resultados
4. Scroll para o botão antes de clicar → campo perde foco (blur), valor é limpo
5. Click via `document.getElementById('buttonSubmit').click()` sem scroll → campo preservado mas busca continua vazia
6. Reset de todos os `select[id^='Filters_']` para índice 0 (limpar filtro de Status) → sem efeito
7. Esperar `blockUI` desaparecer antes de ler tabela → sem efeito
8. `jQuery(el).trigger('change')` nos selects → sem efeito

### Hipótese não testada:
Talvez a busca precise ser submetida via **formulário** (POST/GET com serialização completa), e o `button.click()` via JS não dispare o handler jQuery correto que submete o form via AJAX.

---

## Stack confirmada (da ficha técnica)

- jQuery + Bootstrap 3
- `bootstrap-multiselect`, `bootstrap-select`
- `blockUI` (overlay de loading AJAX)
- `toastr` (mensagens de sucesso/erro)
- ASP.NET MVC backend

---

## Fluxo recomendado pela ficha técnica (§4.4)

```
1. Navegar para /processo/list?cache=false
2. Preencher #Filters_idList com ID (element.value = id; dispatchEvent('change'))
3. Clicar #buttonSubmit
4. Aguardar blockUI desaparecer
5. Clicar link a[href="/processo/details/{id}"] na tabela
```

---

## Código atual — `automation/browser.py`

```python
"""
Inicializacao do navegador, login no ELAW e busca de pasta.

Sempre usa a tela de busca em /processo/list?cache=false.
Decide o campo automaticamente:
  - ID interno (numero puro, ex: 1082658) -> #Filters_idList  (fallback: Protocolo)
  - Numero do processo (com pontos/hifens/letras) -> #Filters_Protocolo

Apos a busca, le o href do link de resultado e navega direto (mais
confiavel que tentar clicar).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

LOG = logging.getLogger("elaw.browser")

ELAW_URL = "https://viseuadv.elawio.com.br"
LOGIN_URL = ELAW_URL + "/Auth/Login"
CONSULTA_URL = ELAW_URL + "/processo/list?cache=false"

# Login
SEL_LOGIN_USER = "#Email"
SEL_LOGIN_PASS = "#Password"
SEL_LOGIN_SUBMIT = "#btn-login"

# Menu de navegacao: Processos > Consultar
SEL_MENU_PROCESSOS = "a:has-text('Processos')"
SEL_SUBMENU_CONSULTAR = "ul.dropdown-menu a:has-text('Consultar')"

# Campos de busca
SEL_BUSCA_INPUT_ID = "#Filters_idList"
SEL_BUSCA_INPUT_PROTOCOLO = "#Filters_Protocolo"
SEL_BUSCA_SUBMIT = "#buttonSubmit"

# Painel de filtros (colapsavel em algumas versoes do ELAW)
SEL_FILTROS_TOGGLE = (
    ".panel-heading[data-toggle='collapse'], "
    "[href='#collapseFilter'], "
    "[data-target='#collapseFilter']"
)

# Marcador da pagina de detalhes
SEL_PASTA_ABERTA = "a[data-toggle='tab'][href='#box-dadosprincipais']"

# Detecta ID interno (numero puro) vs numero de processo
RE_ID_INTERNO = re.compile(r"^\d+$")

# JS que le o estado atual da tabela de resultados
_JS_LER_TABELA = """
(idBuscado) => {
    const tables = [...document.querySelectorAll('table.table-hover')]
        .filter(t => t.offsetParent !== null);
    if (tables.length === 0) {
        const anyTable = [...document.querySelectorAll('table')]
            .filter(t => t.offsetParent !== null && t.querySelector('tbody'));
        if (anyTable.length === 0) return null;
        tables.push(anyTable[0]);
    }
    const tbl = tables[0];
    const emptyCell = tbl.querySelector('.dataTables_empty, td.dataTables_empty');
    if (emptyCell && emptyCell.offsetParent !== null) {
        return {empty: true, texto: emptyCell.textContent.trim()};
    }
    const trs = [...tbl.querySelectorAll('tbody tr')]
        .filter(tr => tr.offsetParent !== null);
    if (trs.length === 0) {
        return {empty: true, texto: 'Nenhum registro localizado'};
    }
    const firstTd = trs[0].querySelector('td[colspan], td.dataTables_empty');
    if (firstTd) return {empty: true, texto: firstTd.textContent.trim()};
    const firstText = trs[0].textContent.trim().toLowerCase();
    if (firstText.includes('nenhum') || firstText.includes('no record')) {
        return {empty: true, texto: trs[0].textContent.trim()};
    }
    const links = [...trs[0].querySelectorAll("a[href*='/processo/details/']")];
    if (links.length === 0) return null;
    const exato = links.find(a => a.textContent.trim() === String(idBuscado));
    const escolhido = exato || links.find(a => a.textContent.trim().length > 0) || links[0];
    return {
        count: trs.length,
        href: escolhido.getAttribute('href'),
        texto_link: escolhido.textContent.trim().slice(0, 40)
    };
}
"""


@dataclass
class Credentials:
    user: str
    password: str


class ElawSession:
    """Sessao Playwright autenticada no ELAW."""

    def __init__(self, credentials, headless=False, timeout_ms=30000):
        self.credentials = credentials
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.debug_screenshot_fn: Optional[Callable[[str, bytes], None]] = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
                  "--disable-gpu","--disable-extensions","--disable-default-apps"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,
        )
        self._context.set_default_timeout(self.timeout_ms)
        self._page = self._context.new_page()

    def close(self):
        try:
            if self._context: self._context.close()
            if self._browser: self._browser.close()
            if self._pw: self._pw.stop()
        except Exception as e:
            LOG.warning("Erro ao fechar: %s", e)

    @property
    def page(self):
        if not self._page:
            raise RuntimeError("Sessao nao iniciada.")
        return self._page

    def _debug_shot(self, label: str):
        if self.debug_screenshot_fn is None:
            return
        try:
            png = self._page.screenshot(timeout=5000)
            self.debug_screenshot_fn(label, png)
        except Exception as e:
            LOG.debug("debug_shot falhou (%s): %s", label, e)

    # =========================================================================
    # BLOCO DE LOGIN — NAO MODIFICAR
    # Testado e aprovado em 06/05/2026. Funciona com deteccao anti-bot do ELAW.
    # =========================================================================
    def login(self):
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        self.page.wait_for_selector(SEL_LOGIN_USER, state="visible", timeout=15000)
        self.page.click(SEL_LOGIN_USER)
        self.page.fill(SEL_LOGIN_USER, "")
        self.page.type(SEL_LOGIN_USER, self.credentials.user, delay=50)
        self.page.click(SEL_LOGIN_PASS)
        self.page.fill(SEL_LOGIN_PASS, "")
        self.page.type(SEL_LOGIN_PASS, self.credentials.password, delay=50)
        self.page.click(SEL_LOGIN_SUBMIT)
        try:
            self.page.wait_for_function(
                "() => !location.pathname.toLowerCase().includes('/auth/login')",
                timeout=self.timeout_ms,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError("Login nao concluiu a tempo (timeout).")
        LOG.info("Login efetuado. URL: %s", self.page.url)
    # =========================================================================
    # FIM DO BLOCO DE LOGIN — NAO MODIFICAR
    # =========================================================================

    def _navegar_para_consulta(self):
        if "/auth/login" in self.page.url.lower():
            raise RuntimeError("Sessao expirou")
        if "/processo/list" in self.page.url.lower():
            self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
            return
        try:
            self.page.hover(SEL_MENU_PROCESSOS)
            self.page.wait_for_selector(SEL_SUBMENU_CONSULTAR, state="visible", timeout=5000)
            self.page.click(SEL_SUBMENU_CONSULTAR)
            self.page.wait_for_url("**/processo/list**", timeout=self.timeout_ms)
        except Exception as e:
            LOG.warning("Menu falhou (%s) — URL direta", e)
            self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")

    def _garantir_filtros_visiveis(self, sel_campo: str):
        try:
            self.page.wait_for_selector(sel_campo, state="visible", timeout=5000)
            return
        except Exception:
            pass
        toggle = self.page.locator(SEL_FILTROS_TOGGLE)
        if toggle.count() > 0:
            toggle.first.click()
            self.page.wait_for_timeout(800)
        try:
            self.page.wait_for_selector(sel_campo, state="visible", timeout=8000)
        except PlaywrightTimeoutError:
            inputs_info = self.page.evaluate(
                """() => [...document.querySelectorAll('input[id], select[id]')]
                    .map(el => ({id: el.id, visible: el.offsetParent !== null}))
                    .slice(0, 20)"""
            )
            raise RuntimeError(
                f"Campo '{sel_campo}' nao encontrado. "
                f"Inputs: {[i['id'] for i in inputs_info if i['id']]}"
            )

    def _limpar_todos_filtros(self):
        """Limpa todos os filtros incluindo StatusId para mostrar Ativo e Encerrado."""
        self.page.evaluate(
            """() => {
                document.querySelectorAll("input[id^='Filters_']").forEach(el => {
                    if (el.type !== 'checkbox' && el.type !== 'radio') {
                        el.value = '';
                        el.dispatchEvent(new Event('input',  {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                });
                document.querySelectorAll("select[id^='Filters_']").forEach(el => {
                    el.selectedIndex = 0;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    if (window.jQuery) {
                        try { window.jQuery(el).trigger('change'); } catch(e) {}
                    }
                });
            }"""
        )

    def _preencher_campo_busca(self, sel_campo: str, valor: str):
        loc = self.page.locator(sel_campo).first
        loc.click(timeout=5000)
        loc.fill("")
        loc.type(valor, delay=40)
        self.page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return;
                el.dispatchEvent(new Event('input',  {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }""",
            sel_campo,
        )
        valor_atual = self.page.evaluate(
            "(sel) => { const el = document.querySelector(sel); return el ? el.value : ''; }",
            sel_campo,
        )
        LOG.info("Campo '%s': esperado='%s' lido='%s'", sel_campo, valor, valor_atual)

    def _clicar_localizar(self):
        """Clica #buttonSubmit via JS para nao mover foco (evita blur limpar o campo).
        HTML confirmado: <input class="btn btn-primary btn-sm" type="button"
                                value="Localizar" id="buttonSubmit">
        """
        clicou = self.page.evaluate(
            """() => {
                const btn = document.getElementById('buttonSubmit');
                if (btn) { btn.click(); return true; }
                return false;
            }"""
        )
        if not clicou:
            raise RuntimeError("Botao Localizar (id=buttonSubmit) nao encontrado")
        LOG.info("Botao Localizar clicado via JS")

    def _aguardar_blockui(self, timeout_ms: int = 30000):
        """Aguarda overlay blockUI desaparecer (confirma fim do AJAX)."""
        try:
            self.page.wait_for_selector(".blockUI, .blockOverlay", state="attached", timeout=3000)
        except Exception:
            pass
        for sel in [".blockUI", ".blockOverlay"]:
            try:
                self.page.wait_for_selector(sel, state="detached", timeout=timeout_ms)
                return
            except Exception:
                pass

    def _executar_busca_e_ler(self, valor: str) -> dict | None:
        self._debug_shot(f"antes_localizar_{valor}")
        self._clicar_localizar()
        self._aguardar_blockui(timeout_ms=min(self.timeout_ms, 30000))
        self._debug_shot(f"apos_localizar_{valor}")

        deadline_iter = max(1, int(self.timeout_ms / 300))
        for i in range(deadline_iter):
            try:
                info = self.page.evaluate(_JS_LER_TABELA, valor)
                if info is not None:
                    return info
            except Exception:
                pass
            self.page.wait_for_timeout(300)
        return None

    def open_pasta(self, pasta_id):
        s = str(pasta_id).strip()
        eh_id_interno = bool(RE_ID_INTERNO.match(s))
        if eh_id_interno:
            campos_tentativa = [
                (SEL_BUSCA_INPUT_ID, "ID interno"),
                (SEL_BUSCA_INPUT_PROTOCOLO, "Numero do Processo"),
            ]
        else:
            campos_tentativa = [
                (SEL_BUSCA_INPUT_PROTOCOLO, "Numero do Processo"),
                (SEL_BUSCA_INPUT_ID, "ID interno"),
            ]

        LOG.info("Buscando pasta '%s'", s)
        self._navegar_para_consulta()
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(600)
        self._debug_shot(f"consulta_carregada_{s}")

        info = None
        ultimo_erro = ""

        for sel_campo, nome_campo in campos_tentativa:
            try:
                self._garantir_filtros_visiveis(sel_campo)
            except RuntimeError as e:
                ultimo_erro = str(e)
                continue

            self._limpar_todos_filtros()
            self._preencher_campo_busca(sel_campo, s)
            self._debug_shot(f"campo_preenchido_{s}_{nome_campo.replace(' ', '_')}")

            info = self._executar_busca_e_ler(s)
            self._debug_shot(f"resultado_busca_{s}_{nome_campo.replace(' ', '_')}")

            if info is None:
                ultimo_erro = "Tabela nao respondeu"
                self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
                continue

            if info.get("empty"):
                ultimo_erro = f"Nenhum resultado com campo '{nome_campo}'"
                self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
                continue

            break
        else:
            raise RuntimeError(f"Pasta '{s}' nao encontrada no sistema ({ultimo_erro})")

        if info.get("count", 1) > 1:
            raise RuntimeError(f"Busca por '{s}' retornou {info['count']} resultados")

        href = info["href"]
        if href.startswith("/"):
            href = ELAW_URL + href
        self.page.goto(href, wait_until="domcontentloaded")

        try:
            self.page.wait_for_selector(SEL_PASTA_ABERTA, timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            self._debug_shot(f"pasta_nao_abriu_{s}")
            raise RuntimeError(f"Pasta '{s}' nao abriu apos navegacao")

        return self.page
```

---

## Código atual — `automation/capa.py`

```python
"""
Atividade: Ajuste de Capa.
Edita campos cadastrais na aba "Geral" do registro da pasta.
Navega directamente para /processo/edit/<id> lendo o href do DOM.
"""

from __future__ import annotations
import logging
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

LOG = logging.getLogger("elaw.capa")

ELAW_URL = "https://viseuadv.elawio.com.br"
SEL_BTN_SALVAR_CAPA = "#buttonSave"
SEL_TOAST_OK  = "#toast-container .toast-success"
SEL_TOAST_ERROR = "#toast-container .toast-error"


def _field_selector(nome_campo: str) -> str:
    """XPath que tolera labels com asterisco (ex: 'Número do Processo *')."""
    return (
        f"xpath=//label[contains(normalize-space(), '{nome_campo}')]"
        f"/following::*[self::input or self::select or self::textarea][1]"
    )


def _abrir_edicao(page: Page):
    """Lê href do link Editar no DOM e navega directamente — sem abrir dropdown."""
    edit_href = page.evaluate(
        """() => {
            const link = document.querySelector("a[href*='/processo/edit/']");
            return link ? link.getAttribute('href') : null;
        }"""
    )
    if not edit_href:
        raise RuntimeError("Link 'Editar' nao encontrado na pagina de detalhes")
    url = (ELAW_URL + edit_href) if edit_href.startswith("/") else edit_href
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(SEL_BTN_SALVAR_CAPA, timeout=20000)


def run(page: Page, config: dict, row: dict) -> str:
    campos = config.get("campos", [])
    if not campos:
        raise RuntimeError("Nenhum campo configurado")

    _abrir_edicao(page)

    aplicados = []
    for idx, campo in enumerate(campos, 1):
        nome  = (campo.get("nome") or "").strip()
        if not nome:
            raise RuntimeError(f"Campo {idx} sem nome")

        if campo.get("value"):
            valor = str(campo["value"])
        elif campo.get("coluna"):
            col = str(campo["coluna"]).strip().upper()
            valor = str(row.get(col, "")).strip()
            if not valor:
                continue
        else:
            raise RuntimeError(f"Campo '{nome}' sem value nem coluna")

        loc = page.locator(_field_selector(nome)).first
        try:
            tag = loc.evaluate("el => el.tagName.toLowerCase()", timeout=10000)
        except Exception as e:
            raise RuntimeError(f"Campo '{nome}' nao encontrado no formulario") from e

        if tag == "select":
            loc.select_option(label=valor)
            loc.evaluate(
                """el => {
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    if (window.jQuery) {
                        try { window.jQuery(el).trigger('change'); } catch(e) {}
                    }
                }"""
            )
        else:
            loc.fill(valor)
            loc.evaluate(
                """el => {
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }"""
            )
        aplicados.append(f"{nome}={valor}")

    page.click(SEL_BTN_SALVAR_CAPA)
    try:
        page.wait_for_url("**/processo/details*", timeout=10000)
    except Exception:
        if page.locator(SEL_TOAST_ERROR).count() > 0:
            raise RuntimeError("Erro ao salvar a Capa")
        page.wait_for_selector(SEL_TOAST_OK, timeout=8000)

    return f"Capa atualizada: {', '.join(aplicados)}"
```

---

## Pergunta para o programador

**O que o robô faz na busca que pode estar errado?**

1. Preenche `#Filters_idList` via `page.type()` (Playwright) + eventos JS
2. Clica `document.getElementById('buttonSubmit').click()` via `page.evaluate()`
3. Aguarda `.blockUI` desaparecer
4. Lê a tabela `table.table-hover tbody tr` procurando `a[href*='/processo/details/']`

**A busca manual funciona.** O robô não encontra os mesmos IDs.

Possível causa: o `button.click()` via JS não dispara o handler jQuery do `#buttonSubmit` que submete o form AJAX? Se for isso, como disparar correctamente?

---

## IDs de teste confirmados como existentes no ELAW

`558246, 559467, 588028, 588031, 588034, 589505, 590459, 592829, 595750,`  
`1028004, 1028006, 1032303, 1032313, 1032318, 1032323, 1039278, 1040317,`  
`1044916, 1046384, 1049759, 1049771, 1064381, 1065640`

**Encontrados pelo robô:** 589505, 590459, 1032313 (esporadicamente)  
**Não encontrados:** todos os outros
