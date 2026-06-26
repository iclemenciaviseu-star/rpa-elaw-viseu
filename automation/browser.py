"""
Inicializacao do navegador, login no ELAW e busca de pasta.

=============================================================================
ATENCAO — MODULO CONGELADO (aprovado em 06/05/2026)
=============================================================================
Este modulo contem a logica de LOGIN e de BUSCA DE PASTA (open_pasta).
Essas duas fases sao compartilhadas por TODAS as 4 atividades do RPA:
  - Ajuste de Capa   (automation/capa.py)
  - Prazos           (automation/prazos.py)
  - Pedidos          (automation/pedidos.py)
  - Documentos       (automation/documentos.py)

NAO MODIFIQUE este arquivo sem testes completos nos 4 modelos de tarefa.
Qualquer alteracao nos metodos abaixo pode quebrar silenciosamente todas
as atividades:
  - login()
  - _navegar_para_consulta()
  - _garantir_filtros_visiveis()
  - _limpar_todos_filtros()      <- correcao critica multiselect 06/05/2026
  - _preencher_campo_busca()
  - _clicar_localizar()
  - _aguardar_blockui()
  - _executar_busca_e_ler()
  - open_pasta()
  - _JS_LER_TABELA               <- selecao por Tipo="Processo Principal"
=============================================================================

Estrategia de busca:
  - ID interno (numero puro, ex: 1082658) -> #Filters_idList  (fallback: Protocolo)
  - Numero do processo (com pontos/hifens/letras) -> #Filters_Protocolo

Quando ha multiplos resultados (ex.: Processo Principal + Recurso com o mesmo
ID), a RPA navega automaticamente para o "Processo Principal" independente
do Status (Ativo ou Encerrado). Se nao houver linha com Tipo="Processo
Principal", usa a primeira linha disponivel como fallback.
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

# ---------------------------------------------------------------------------
# Argumentos do Chromium — uso local Windows
# ---------------------------------------------------------------------------
_CHROMIUM_ARGS = [
    "--no-first-run",
    "--disable-extensions",
    "--disable-default-apps",
    "--mute-audio",
    "--hide-scrollbars",
]

# ---------------------------------------------------------------------------
# Recursos bloqueados no BrowserContext
# ---------------------------------------------------------------------------
# Regex de extensao (processado em C++ pelo Playwright — sem overhead Python
# para cada request). So chama Python quando o URL bate o padrao.
#
# SVG EXCLUIDO intencionalmente: o ELAW carrega icones de acao (Editar, etc.)
# via fetch('/api/...icon.svg'). Bloquear por extensao abortava esses requests,
# impedindo o botao Editar de aparecer no DOM.
# SVG e um formato de texto e nao contribui significativamente para a memoria.
#
# Nao usar ctx.route("**/*", handler) com route.continue_(): obriga Python a
# processar CADA request (incluindo polling AJAX do ELAW), criando overhead
# que trava o processo a partir do ~13o caso.
_RE_BLOQUEAR = re.compile(
    r"\.(png|jpg|jpeg|gif|webp|ico|bmp|woff|woff2|ttf|eot|otf"
    r"|mp4|mp3|wav|webm|ogg)(\?[^/]*)?$",
    re.IGNORECASE,
)

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
# =============================================================================
# NAO MODIFICAR — logica de leitura de tabela e selecao de Processo Principal
# Aprovado e congelado em 06/05/2026.
# =============================================================================
_JS_LER_TABELA = """
(idBuscado) => {
    // Procura tabela visivel com a classe table-hover
    const tables = [...document.querySelectorAll('table.table-hover')]
        .filter(t => t.offsetParent !== null);
    if (tables.length === 0) {
        // Tenta qualquer tabela visivel como fallback
        const anyTable = [...document.querySelectorAll('table')]
            .filter(t => t.offsetParent !== null && t.querySelector('tbody'));
        if (anyTable.length === 0) return null;
        tables.push(anyTable[0]);
    }
    const tbl = tables[0];

    // Mensagem de "sem resultados" dentro da tabela
    const emptyCell = tbl.querySelector('.dataTables_empty, td.dataTables_empty');
    if (emptyCell && emptyCell.offsetParent !== null) {
        return {empty: true, texto: emptyCell.textContent.trim()};
    }

    const trs = [...tbl.querySelectorAll('tbody tr')]
        .filter(tr => tr.offsetParent !== null);

    // Tabela sem linhas = sem resultado
    if (trs.length === 0) {
        // Verifica mensagem fora da tabela
        const msgs = [...document.querySelectorAll(
            '.alert, .alert-info, .alert-warning, .no-results, [class*="empty"]'
        )].filter(el => el.offsetParent !== null);
        const msgText = msgs.map(el => el.textContent.trim()).join(' ').toLowerCase();
        if (msgText.includes('nenhum') || msgText.includes('no record') || msgText.includes('sem result')) {
            return {empty: true, texto: msgs[0] ? msgs[0].textContent.trim() : 'Nenhum resultado'};
        }
        // Tabela vazia sem mensagem => ainda carregando ou realmente vazio
        return {empty: true, texto: 'Nenhum registro localizado'};
    }

    // Detecta linha de "nenhum registro" via colspan ou texto
    const firstTd = trs[0].querySelector('td[colspan], td.dataTables_empty');
    if (firstTd) return {empty: true, texto: firstTd.textContent.trim()};
    const firstText = trs[0].textContent.trim().toLowerCase();
    if (
        firstText.includes('nenhum') ||
        firstText.includes('no record') ||
        firstText.includes('sem result') ||
        firstText.includes('nao encontrado')
    ) {
        return {empty: true, texto: trs[0].textContent.trim()};
    }

    // -----------------------------------------------------------------------
    // Selecao da linha correta quando ha multiplos resultados
    // -----------------------------------------------------------------------
    // Descobre o indice da coluna "Tipo" no cabecalho
    const ths = [...tbl.querySelectorAll('thead th')];
    const idxTipo = ths.findIndex(th =>
        th.textContent.trim().toLowerCase() === 'tipo'
    );

    // Funcao auxiliar: extrai o texto da celula "Tipo" de uma linha
    const getTipo = (tr) => {
        if (idxTipo < 0) return '';
        const tds = tr.querySelectorAll('td');
        return tds[idxTipo] ? tds[idxTipo].textContent.trim().toLowerCase() : '';
    };

    // Prefere a linha cujo tipo seja "Processo Principal" (case-insensitive)
    // Fallback: primeira linha com link de detalhes
    let linhaSelecionada = null;
    let tipoDaLinha = '';

    for (const tr of trs) {
        const tipo = getTipo(tr);
        const temLink = tr.querySelector("a[href*='/processo/details/']");
        if (!temLink) continue;

        if (tipo.includes('processo principal')) {
            linhaSelecionada = tr;
            tipoDaLinha = getTipo(tr);
            break;  // encontrou o principal — para aqui
        }
        if (!linhaSelecionada) {
            // guarda o primeiro com link como fallback
            linhaSelecionada = tr;
            tipoDaLinha = tipo;
        }
    }

    if (!linhaSelecionada) return null;

    // Pega os links de detalhes dentro da linha escolhida
    const links = [...linhaSelecionada.querySelectorAll("a[href*='/processo/details/']")];
    if (links.length === 0) return null;

    // Acha o link cujo TEXTO bate com o id buscado, senao pega o que tem texto preenchido
    const exato = links.find(a => a.textContent.trim() === String(idBuscado));
    const escolhido = exato || links.find(a => a.textContent.trim().length > 0) || links[0];
    return {
        count: trs.length,
        tipo: tipoDaLinha,
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
        # Callback opcional para envio de screenshots de debug: fn(label, png_bytes)
        self.debug_screenshot_fn: Optional[Callable[[str, bytes], None]] = None
        # Flag: True quando a pagina de consulta acabou de ser carregada do servidor.
        # Nesse estado os filtros estao no estado padrao (sem lixo de casos anteriores),
        # por isso podemos pular _limpar_todos_filtros() e economizar ~10s por caso.
        self._filtros_frescos: bool = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        LOG.info("Iniciando Playwright...")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=_CHROMIUM_ARGS,
        )
        self._context = self._criar_context()
        self._page = self._context.new_page()

    def close(self):
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception as e:
            LOG.warning("Erro ao fechar Playwright: %s", e)

    @property
    def page(self):
        if not self._page:
            raise RuntimeError("Sessao nao iniciada. Chame start() primeiro.")
        return self._page

    @property
    def context(self):
        if not self._context:
            raise RuntimeError("Sessao nao iniciada.")
        return self._context

    def _criar_context(self) -> BrowserContext:
        """Cria BrowserContext com bloqueio de recursos desnecessarios.

        Todas as criações de contexto (start, recycle_context, recycle_browser)
        passam por aqui para garantir consistencia.

        Bloqueio por regex de extensao (C++) — sem SVG:
          - PNG, JPG, GIF, WEBP, ICO, BMP  (imagens rasterizadas)
          - WOFF, WOFF2, TTF, EOT, OTF     (fontes)
          - MP4, MP3, WAV, WEBM, OGG       (media)
        SVG excluido: o ELAW usa fetch('/api/...svg') para icones de acao.
        Scripts, CSS, XHR, fetch continuam normalmente.
        """
        ctx = self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            accept_downloads=True,
        )
        ctx.set_default_timeout(self.timeout_ms)
        ctx.route(_RE_BLOQUEAR, lambda route: route.abort())
        return ctx

    def _debug_shot(self, label: str):
        """Tira screenshot de debug e chama o callback se definido."""
        if self.debug_screenshot_fn is None:
            return
        try:
            png = self._page.screenshot(timeout=5000)
            self.debug_screenshot_fn(label, png)
        except Exception as e:
            LOG.debug("debug_shot falhou (%s): %s", label, e)

    def recycle_page(self):
        """Fecha a Page atual e abre uma nova no MESMO contexto.

        Mantem cookies/sessao (login continua valido via BrowserContext),
        mas libera memoria de DOM, listeners e AJAX residual acumulados.

        No novo fluxo de 'aba por caso', este metodo e chamado no INICIO
        de cada caso (abre aba limpa). O close_page() e chamado no FINALLY
        para fechar a aba ao terminar — garantindo que nenhum residuo fica
        na memoria entre casos.
        """
        try:
            if self._page and not self._page.is_closed():
                self._page.close()
        except Exception as e:
            LOG.warning("Erro ao fechar page antiga: %s", e)
        self._page = self._context.new_page()
        self._context.set_default_timeout(self.timeout_ms)

    def close_page(self):
        """Fecha a Page atual sem abrir outra (chamado no finally de cada caso).

        A proxima chamada a recycle_page() abrira uma nova aba limpa antes
        do proximo caso comecar.
        """
        try:
            if self._page and not self._page.is_closed():
                self._page.close()
                self._page = None
        except Exception as e:
            LOG.warning("Erro ao fechar page: %s", e)

    def recycle_context(self):
        """Fecha o BrowserContext inteiro e cria um novo, refazendo login.

        Reciclagem mais profunda que recycle_page() — limpa cookies, cache,
        listeners e service workers acumulados ao longo do tempo.
        Re-faz login automaticamente no novo contexto.

        Use a cada ~25 casos em ambientes com pouca RAM (ex.: Render free).
        """
        LOG.info("Reciclando BrowserContext (limpa cookies/cache, re-login)...")
        try:
            if self._context:
                self._context.close()
        except Exception as e:
            LOG.warning("Erro ao fechar context antigo: %s", e)
        self._context = self._criar_context()
        self._page = self._context.new_page()
        self.login()
        LOG.info("Context reciclado e re-login efetuado.")

    def recycle_browser(self):
        """Reciclagem profunda: fecha o Chromium inteiro e relanca.

        Use a cada ~100 casos para garantir limpeza completa de memoria
        do processo Chromium. Mais lento (~5-10s) mas evita memory creep
        que mata o Chromium em sessoes longas (ex.: travas em ~8-20 casos
        observadas no Render free tier antes desta correcao).
        """
        LOG.info("Reciclagem profunda: fechando Chromium e relancando...")
        try:
            if self._context:
                self._context.close()
        except Exception as e:
            LOG.warning("Erro ao fechar context: %s", e)
        try:
            if self._browser:
                self._browser.close()
        except Exception as e:
            LOG.warning("Erro ao fechar browser: %s", e)

        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=_CHROMIUM_ARGS,
        )
        self._context = self._criar_context()
        self._page = self._context.new_page()
        self.login()
        LOG.info("Browser reciclado e re-login efetuado.")

    def health_check(self) -> bool:
        """Verifica rapidamente se o Chromium ainda responde.

        Retorna True se OK, False se travado/morto. Permite detectar
        Chromium congelado SEM esperar o timeout de 60s do proximo
        comando — economiza minutos em runs longos.

        IMPORTANTE: espera a pagina estabilizar antes de avaliar.
        Sem esse wait, o evaluate falha com "Execution context was
        destroyed" durante navegacoes normais (ex.: redirect apos Salvar),
        gerando falsos negativos que disparam recycles desnecessarios.
        """
        if not self._page:
            return False
        try:
            # Aguarda a pagina estabilizar (ignora erros de rede/timeout aqui)
            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass  # se nao estabilizar em 3s, tenta o evaluate mesmo assim
            # page.evaluate() nao aceita o parametro timeout em todas as versoes
            # do Playwright — usamos set_default_timeout temporariamente.
            self._page.set_default_timeout(5000)
            self._page.evaluate("1+1")
            return True
        except Exception as e:
            LOG.warning("Health check falhou: %s", e)
            return False
        finally:
            # Restaura o timeout padrao do contexto independente do resultado
            try:
                self._page.set_default_timeout(self.timeout_ms)
            except Exception:
                pass

    # =========================================================================
    # BLOCO DE LOGIN — NAO MODIFICAR
    # Testado e aprovado em 06/05/2026. Funciona com deteccao anti-bot do ELAW.
    # Qualquer alteracao neste metodo pode quebrar o login. Nao alterar.
    # =========================================================================
    def login(self):
        """Autentica no ELAW. Detecta sucesso pela mudanca de URL.

        ATENCAO: metodo estavel e aprovado — NAO ALTERAR.
        Usa page.type() com delay=50ms para simular digitacao humana e evitar
        bloqueio anti-bot do ELAW. page.fill() foi testado e rejeitado pelo sistema.
        """
        LOG.info("Acessando pagina de login...")
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            # Aguarda campos visiveis
            self.page.wait_for_selector(SEL_LOGIN_USER, state="visible", timeout=15000)
            # Clica e digita tecla a tecla (mais natural, evita rejeicao anti-bot)
            self.page.click(SEL_LOGIN_USER)
            self.page.fill(SEL_LOGIN_USER, "")
            self.page.type(SEL_LOGIN_USER, self.credentials.user, delay=50)
            self.page.click(SEL_LOGIN_PASS)
            self.page.fill(SEL_LOGIN_PASS, "")
            self.page.type(SEL_LOGIN_PASS, self.credentials.password, delay=50)
            self.page.click(SEL_LOGIN_SUBMIT)
        except PlaywrightTimeoutError as e:
            raise RuntimeError("Nao consegui preencher os campos de login.") from e

        try:
            self.page.wait_for_function(
                "() => !location.pathname.toLowerCase().includes('/auth/login')",
                timeout=self.timeout_ms,
            )
        except PlaywrightTimeoutError:
            erro_visivel = ""
            # Seletores expandidos para capturar mensagens de erro do ELAW
            selectors = [
                ".validation-summary-errors", ".alert-danger", ".text-danger",
                ".error", ".help-block", "[role='alert']", ".message-error",
                ".login-error", ".field-validation-error", ".error-message",
            ]
            for sel in selectors:
                try:
                    loc = self.page.locator(sel)
                    if loc.count() > 0:
                        txt = loc.first.inner_text().strip()
                        if txt:
                            erro_visivel = txt[:200]
                            break
                except Exception:
                    pass

            # Captura screenshot do estado da tela de login para diagnostico
            self._debug_shot("login_timeout")

            if erro_visivel:
                raise RuntimeError("Login negado pelo ELAW: " + erro_visivel)
            raise RuntimeError(
                "Login nao concluiu a tempo (timeout). "
                "Possiveis causas: senha incorreta, captcha, anti-bot ou ELAW lento. "
                "Defina DEBUG_SCREENSHOTS=1 para ver a tela no momento do erro."
            )

        LOG.info("Login efetuado com sucesso. URL atual: %s", self.page.url)
    # =========================================================================
    # FIM DO BLOCO DE LOGIN — NAO MODIFICAR
    # =========================================================================

    # =========================================================================
    # BLOCO DE BUSCA — NAO MODIFICAR
    # Testado e aprovado em 06/05/2026. Base comum para todas as 4 atividades.
    # =========================================================================

    def _navegar_para_consulta(self):
        """Processos > Consultar — vai direto para CONSULTA_URL sempre que possivel.

        Otimizacao: com aba nova por caso, a pagina esta sempre em branco ou
        fora do ELAW. Tentar o menu nesse estado desperdicava 5s de timeout
        antes do fallback para URL direta. Agora detectamos isso e vamos
        direto — economiza 5s por caso (319 casos = ~26 min no total).
        """
        url = self.page.url.lower()

        if "/auth/login" in url:
            raise RuntimeError("Sessao expirou — pagina redirecionou para o login")

        # Pagina em branco (aba nova) ou fora do ELAW — nao tenta menu, vai direto
        if not url.startswith(ELAW_URL.lower()):
            self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
            self._filtros_frescos = True
            return

        # Ja na pagina de consulta — recarrega para estado limpo
        if "/processo/list" in url:
            self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
            self._filtros_frescos = True
            LOG.info("Ja na pagina de consulta — recarregada")
            return

        # Dentro do ELAW mas em outra pagina — tenta menu, fallback URL direta
        try:
            self.page.hover(SEL_MENU_PROCESSOS, timeout=5000)
            self.page.wait_for_selector(SEL_SUBMENU_CONSULTAR, state="visible", timeout=5000)
            self.page.click(SEL_SUBMENU_CONSULTAR)
            self.page.wait_for_url("**/processo/list**", timeout=self.timeout_ms)
            self._filtros_frescos = True
            LOG.info("Navegou via menu: Processos > Consultar")
        except Exception as e:
            LOG.warning("Menu falhou (%s) — usando URL direta", e)
            self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
            self._filtros_frescos = True

    def _garantir_filtros_visiveis(self, sel_campo: str):
        """Expande o painel de filtros colapsado se necessario."""
        try:
            self.page.wait_for_selector(sel_campo, state="visible", timeout=5000)
            return
        except Exception:
            pass

        # Tenta toggle de colapso
        toggle = self.page.locator(SEL_FILTROS_TOGGLE)
        if toggle.count() > 0:
            toggle.first.click()
            LOG.info("Painel de filtros expandido via toggle")
            self.page.wait_for_timeout(800)

        # Ultima tentativa
        try:
            self.page.wait_for_selector(sel_campo, state="visible", timeout=8000)
        except PlaywrightTimeoutError:
            # Loga todos os inputs disponiveis para diagnostico
            inputs_info = self.page.evaluate(
                """() => [...document.querySelectorAll('input[id], select[id]')]
                    .map(el => ({id: el.id, type: el.type, visible: el.offsetParent !== null}))
                    .slice(0, 20)"""
            )
            LOG.warning("Campo '%s' nao encontrado. Inputs na pagina: %s", sel_campo, inputs_info)
            self._debug_shot("filtros_nao_encontrados")
            raise RuntimeError(
                f"Campo de busca '{sel_campo}' nao encontrado na pagina de consulta. "
                f"Inputs disponiveis: {[i['id'] for i in inputs_info if i['id']]}"
            )

    def _limpar_todos_filtros(self):
        """Limpa todos os filtros da pagina de consulta.

        CORRECAO CRITICA (06/05/2026):
        Para <select multiple>, selectedIndex=0 NAO desmarca — SELECIONA o primeiro
        item (ex.: "Aline Melo de Oliveira"), envenenando a busca silenciosamente.
        Multiselects afetados: Filters_Responsaveis, Filters_Escritorios,
        Filters_Estados, Filters_Clientes, Filters_UsuariosCadastro.
        A solucao e iterar todas as options e setar selected=false.
        """
        self.page.evaluate(
            """() => {
                // 1. Limpa inputs de texto / numero / data
                document.querySelectorAll("input[id^='Filters_']").forEach(el => {
                    if (el.type !== 'checkbox' && el.type !== 'radio') {
                        el.value = '';
                        el.dispatchEvent(new Event('input',  {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                });

                // 2. Limpa selects — diferenciando single de multiple
                document.querySelectorAll("select[id^='Filters_']").forEach(el => {
                    if (el.multiple) {
                        // MULTISELECT: desmarcar TODAS as opcoes individualmente
                        // (selectedIndex=0 num multiselect SELECIONA — nao desmarca)
                        Array.from(el.options).forEach(o => { o.selected = false; });
                    } else {
                        // SINGLE SELECT: voltar para primeira opcao (Todos / -- SELECIONE --)
                        el.selectedIndex = 0;
                    }
                    el.dispatchEvent(new Event('change', {bubbles: true}));

                    // Notifica listeners jQuery do change (sem refresh visual —
                    // multiselect/selectpicker refresh reconstroi toda a UI e
                    // leva 1-2s por widget; o submit usa o <select> nativo,
                    // nao o widget visual, entao o refresh e desnecessario).
                    if (window.jQuery) {
                        try { window.jQuery(el).trigger('change'); } catch(e) {}
                    }
                });

                // 3. Desmarca checkboxes
                document.querySelectorAll("input[id^='Filters_'][type='checkbox']:checked").forEach(el => {
                    el.checked = false;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                });
            }"""
        )
        LOG.info("Filtros limpos (multiselects desmarcados, singles resetados)")

    def _preencher_campo_busca(self, sel_campo: str, valor: str):
        """Preenche o campo de busca especificado com o valor.
        Verifica que o valor foi aceite antes de retornar.
        """
        loc = self.page.locator(sel_campo).first
        loc.click(timeout=15_000)  # 15s — pagina pode ter overlay residual no Render
        loc.fill(valor)            # fill() e atomico e rapido — type(delay=40) era ~40ms/char
                                   # desnecessario nesta pagina interna (sem anti-bot)

        # Dispara eventos para frameworks JS (jQuery / AngularJS / Knockout)
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

        # Verifica o valor lido de volta
        valor_atual = self.page.evaluate(
            "(sel) => { const el = document.querySelector(sel); return el ? el.value : ''; }",
            sel_campo,
        )
        LOG.info("Campo '%s': esperado='%s' lido='%s'", sel_campo, valor, valor_atual)

        if valor_atual.strip() != valor.strip():
            LOG.warning("Valor nao aceite via type() — tentando setter nativo JS")
            self.page.evaluate(
                """(args) => {
                    const el = document.querySelector(args.sel);
                    if (!el) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    setter.call(el, args.val);
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                {"sel": sel_campo, "val": valor},
            )
            valor_atual2 = self.page.evaluate(
                "(sel) => { const el = document.querySelector(sel); return el ? el.value : ''; }",
                sel_campo,
            )
            LOG.info("Apos setter JS: lido='%s'", valor_atual2)

    def _clicar_localizar(self):
        """Clica no botao Localizar via JavaScript directo.

        Usar JS evita que o scroll ou a mudanca de foco dispare um evento
        'blur' no campo de pesquisa, que poderia limpar o valor no ELAW.
        HTML confirmado: <input type="button" id="buttonSubmit" value="Localizar">
        """
        clicou = self.page.evaluate(
            """() => {
                const btn = document.getElementById('buttonSubmit');
                if (btn) { btn.click(); return true; }
                // Fallback: qualquer input/button com value ou texto "Localizar"
                const fallback = [...document.querySelectorAll(
                    'input[value="Localizar"], button'
                )].find(el => el.textContent.trim() === 'Localizar' || el.value === 'Localizar');
                if (fallback) { fallback.click(); return 'fallback'; }
                return false;
            }"""
        )
        if not clicou:
            raise RuntimeError(
                "Botao 'Localizar' (id=buttonSubmit) nao encontrado na pagina de busca"
            )
        LOG.info("Botao Localizar clicado via JS (resultado: %s)", clicou)

    def _aguardar_blockui(self, timeout_ms: int = 30000):
        """Aguarda o overlay blockUI (loading) do ELAW desaparecer.

        O ELAW usa jQuery blockUI para cobrir a pagina durante chamadas AJAX.
        Precisamos esperar ele sumir antes de ler os resultados da tabela.
        Seletores confirmados pela ficha tecnica: .blockUI / .blockOverlay
        """
        # Espera aparecer (confirma que o AJAX comecou) — timeout curto
        try:
            self.page.wait_for_selector(
                ".blockUI, .blockOverlay",
                state="attached",
                timeout=3000,
            )
        except Exception:
            pass  # Se nao aparecer, o AJAX pode ter sido rapido

        # Espera desaparecer (AJAX concluido)
        for sel in [".blockUI", ".blockOverlay"]:
            try:
                self.page.wait_for_selector(sel, state="detached", timeout=timeout_ms)
                LOG.info("blockUI desapareceu ('%s') — tabela deve estar actualizada", sel)
                return
            except Exception:
                pass
        LOG.debug("blockUI nao detectado — continuando sem esperar")

    def _executar_busca_e_ler(self, valor: str) -> dict | None:
        """Clica em Localizar, aguarda o blockUI desaparecer e retorna o resultado.

        Fluxo confirmado pela ficha tecnica:
          1. Clicar #buttonSubmit
          2. Aguardar overlay blockUI desaparecer (AJAX concluido)
          3. Ler a tabela de resultados
        """
        self._debug_shot(f"antes_localizar_{valor}")
        self._clicar_localizar()

        # Aguarda o AJAX do ELAW terminar (blockUI overlay)
        self._aguardar_blockui(timeout_ms=min(self.timeout_ms, 30000))
        self._debug_shot(f"apos_localizar_{valor}")

        # Polling da tabela (fallback caso blockUI nao seja detectado)
        deadline_iter = max(1, int(self.timeout_ms / 300))
        for i in range(deadline_iter):
            try:
                info = self.page.evaluate(_JS_LER_TABELA, valor)
                if info is not None:
                    LOG.info("Tabela respondeu na iteracao %d: %s", i, info)
                    return info
            except Exception as e:
                LOG.debug("Iter %d erro JS: %s", i, e)
            self.page.wait_for_timeout(300)

        return None  # timeout sem resposta da tabela

    def open_pasta(self, pasta_id):
        """Abre a pasta via Processos > Consultar.

        Estrategia de busca:
          1. Tenta pelo campo primario (ID interno OU Protocolo conforme o valor)
          2. Se nao encontrar, tenta o campo alternativo como fallback
          3. Navega direto pelo href do resultado
        """
        s = str(pasta_id).strip()
        eh_id_interno = bool(RE_ID_INTERNO.match(s))

        # Campos na ordem de tentativa
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
        self._filtros_frescos = False   # sera setado por _navegar_para_consulta
        self._navegar_para_consulta()

        # Aguarda pagina estabilizar e blockUI sumir antes de interagir.
        # O ELAW pode mostrar overlay de loading ao carregar a pagina de consulta —
        # se tentarmos clicar no campo de busca com overlay ativo, o click trava.
        # Removido wait_for_timeout(600) fixo — _aguardar_blockui ja faz a espera real.
        self.page.wait_for_load_state("domcontentloaded")
        self._aguardar_blockui(timeout_ms=10_000)

        # Screenshot de debug: pagina de consulta carregada
        self._debug_shot(f"consulta_carregada_{s}")

        info = None
        ultimo_erro = ""

        for sel_campo, nome_campo in campos_tentativa:
            LOG.info("  Tentando campo '%s' (%s)", sel_campo, nome_campo)
            try:
                self._garantir_filtros_visiveis(sel_campo)
            except RuntimeError as e:
                ultimo_erro = str(e)
                LOG.warning("  Campo '%s' indisponivel: %s", nome_campo, e)
                continue

            # Limpa filtros antes de preencher — garante que nenhum filtro oculto
            # exclua resultados. Pula quando a pagina e fresca (acaba de ser
            # carregada via goto CONSULTA_URL): o servidor renderiza os filtros
            # em estado padrao, nao ha lixo para limpar — economiza ~10s por caso.
            if self._filtros_frescos:
                LOG.info("Pagina fresca — filtros no estado padrao, pulando limpeza")
                self._filtros_frescos = False
            else:
                self._limpar_todos_filtros()

            self._preencher_campo_busca(sel_campo, s)
            self._debug_shot(f"campo_preenchido_{s}_{nome_campo.replace(' ', '_')}")

            info = self._executar_busca_e_ler(s)
            self._debug_shot(f"resultado_busca_{s}_{nome_campo.replace(' ', '_')}")

            if info is None:
                LOG.warning("  Tabela nao respondeu com campo '%s'", nome_campo)
                ultimo_erro = "Tabela de resultados nao apareceu apos a busca"
                # Recarrega a pagina antes de tentar o proximo campo
                self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
                self._filtros_frescos = True   # pagina recarregada = filtros limpos
                continue

            if info.get("empty"):
                LOG.info("  Nenhum resultado com campo '%s': %s", nome_campo, info.get("texto", ""))
                ultimo_erro = f"Nenhum resultado com campo '{nome_campo}'"
                # Recarrega antes do proximo campo
                self.page.goto(CONSULTA_URL, wait_until="domcontentloaded")
                self._filtros_frescos = True   # pagina recarregada = filtros limpos
                continue

            # Resultado encontrado
            break
        else:
            # Esgotou todos os campos sem resultado
            raise RuntimeError(f"Pasta '{s}' nao encontrada no sistema ({ultimo_erro})")

        if info is None or info.get("empty"):
            raise RuntimeError(f"Pasta '{s}' nao encontrada no sistema")

        if info.get("count", 1) > 1:
            # Quando ha multiplos resultados, o JS ja selecionou o "Processo Principal".
            # Apenas loga um aviso — nao aborta.
            tipo_sel = info.get("tipo", "")
            LOG.warning(
                "Busca retornou %d resultados para '%s'. Usando linha '%s': %s",
                info["count"], s, tipo_sel or "primeiro disponivel", info.get("href", ""),
            )

        # Navega direto para a URL do detalhe.
        # Usa wait_until="commit" (apenas aguarda headers HTTP, quase imediato)
        # em vez de "domcontentloaded" — paginas de detalhe ELAW tem muitos
        # scripts pesados que atrasam o DOMContentLoaded em producao e podem
        # causar hang indefinido sem timeout explicito.
        href = info["href"]
        if href.startswith("/"):
            href = ELAW_URL + href
        LOG.info("Navegando para %s (link='%s')", href, info.get("texto_link", ""))
        try:
            self.page.goto(href, wait_until="commit", timeout=30_000)
        except Exception:
            # Se timeout no commit, tenta mesmo assim — confirmacao abaixo
            # vai verificar se a pasta abriu de outro jeito
            pass

        # Confirma que a pasta abriu verificando a URL (mais robusto que selector).
        # wait_for_url resolve imediatamente se a URL ja bate — seguro chamar
        # mesmo depois de um goto bem-sucedido.
        # Nao depende de elemento especifico que pode variar por tipo de pasta.
        try:
            self.page.wait_for_url("**/processo/details/**", timeout=30_000)
            LOG.info("Pasta '%s' aberta com sucesso (URL confirmada).", s)
        except PlaywrightTimeoutError:
            # URL nao mudou para /processo/details/ — verifica diretamente
            url_atual = self.page.url.lower()
            if "/processo/details/" in url_atual:
                LOG.info("Pasta '%s' aberta (URL ja correta: %s).", s, self.page.url)
            else:
                self._debug_shot(f"pasta_nao_abriu_{s}")
                raise RuntimeError(
                    f"Pasta '{s}' nao abriu apos a navegacao "
                    f"(URL atual: {self.page.url})"
                )

        # Aguarda DOM pronto antes de retornar para a atividade.
        # Com resource blocking, DOMContentLoaded dispara rapido (imagens/fontes
        # sao abortadas imediatamente — nao ficam pendentes).
        # Sem este wait, _abrir_edicao() pode comecar a procurar o link Editar
        # antes do HTML ter sido completamente parseado.
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=30_000)
            LOG.debug("DOM carregado para pasta '%s'.", s)
        except Exception:
            pass  # prossegue mesmo assim — _abrir_edicao() tem timeout proprio

        return self.page
    # =========================================================================
    # FIM DO BLOCO DE BUSCA — NAO MODIFICAR
    # =========================================================================
