"""
Atividade: Ajuste de Capa.

Edita campos cadastrais na aba "Geral" do registro da pasta.
O fluxo de edicao navega directamente para /processo/edit/{id}.

Config esperada:
{
    "campos": [
        {"nome": "Advogado Responsavel", "value": "Dr. Joao Silva", "coluna": None},
        {"nome": "Status",                "value": None,             "coluna": "B"},
    ]
}

O campo "nome" deve ser o TEXTO DO LABEL visivel no formulario ELAW.
Nao e necessario incluir o asterisco (*) — o XPath usa contains().
"""

from __future__ import annotations

import logging
import re
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

LOG = logging.getLogger("elaw.capa")

ELAW_URL        = "https://viseuadv.elawio.com.br"
SEL_BTN_SALVAR  = "#buttonSave"
SEL_TOAST_OK    = "#toast-container .toast-success"
SEL_TOAST_ERROR = "#toast-container .toast-error"

# ---------------------------------------------------------------------------
# Mapa de dependencias entre combos (pai -> filho que precisa repopular)
# Medido no sistema real em 06/05/2026 — ficha-atualizacao-rpa-elaw.md §2
# ---------------------------------------------------------------------------
# Tempo maximo de polling por campo pai (ms)
_TEMPO_ESPERA: dict[str, int] = {
    "EstadoId":       1500,   # afeta CidadeId (~423-1010 ms)
    "AreaDireitoId":  1500,   # afeta SubAreaDireitoId (~245-1095 ms)
    "OrigemId":       1500,   # afeta NaturezaId + JuizadoId (~300-700 ms)
    "ClasseId":        800,   # afeta SubClasseId (~0-500 ms)
    "NaturezaId":     1500,   # afeta JuizadoId indireto
    "GrupoClienteId": 3000,   # cascata pesada — usar networkidle (~2145 ms)
    "_default":        500,   # combo sem cascata conhecida
}

# Campos cujo AJAX e' tao pesado que vale esperar networkidle
_NETWORKIDLE = {"GrupoClienteId"}

# Mapa pai -> seletor CSS do filho que deve repopular
_DEPENDENCIAS: dict[str, str] = {
    "EstadoId":       "#CidadeId",
    "AreaDireitoId":  "#SubAreaDireitoId",
    "OrigemId":       "#NaturezaId",
    "ClasseId":       "#SubClasseId",
    "GrupoClienteId": "#ClienteId",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _field_selector(nome_campo: str) -> str:
    """XPath que localiza input/select/textarea pelo texto do label.

    Prioridade:
      1. Dentro do .form-group pai — evita vazar para grupos vizinhos
      2. following:: excluindo hidden/checkbox/radio — fallback
    """
    return (
        f"xpath=("
        f"//label[contains(normalize-space(), '{nome_campo}')]"
        f"/ancestor::*[contains(@class,'form-group')][1]"
        f"//*[self::select or self::textarea"
        f"    or (self::input and @type!='hidden' and @type!='checkbox' and @type!='radio')][1]"
        f"|"
        f"//label[contains(normalize-space(), '{nome_campo}')]"
        f"/following::*[self::select or self::textarea"
        f"              or (self::input and @type!='hidden' and @type!='checkbox' and @type!='radio')][1]"
        f")[1]"
    )


def _aguardar_jquery_idle(page: Page, timeout_ms: int = 5_000) -> None:
    """Aguarda jQuery.active == 0 (nenhum AJAX pendente no ELAW).

    Mais confiavel que page.wait_for_load_state('networkidle') porque:
    - networkidle nunca resolve se a pagina tem polling de fundo (ELAW tem)
    - jQuery.active e' o contador interno que o proprio blockUI do ELAW usa
    Termina assim que nao ha requisicoes em curso, ou apos timeout_ms.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        ativo = page.evaluate(
            "() => (typeof jQuery !== 'undefined') ? jQuery.active : 0"
        )
        if ativo == 0:
            LOG.debug("jQuery.active == 0 — sem AJAX pendente")
            return
        page.wait_for_timeout(150)
    LOG.debug("jQuery.active ainda > 0 apos %d ms — prosseguindo mesmo assim", timeout_ms)


def _esperar_filho_popular(page: Page, child_sel: str, timeout_ms: int) -> bool:
    """Polling: aguarda o <select> filho repopular apos AJAX de cascata.

    Criterio: numero de opcoes mudou OU surgiu opcao real (value != '').
    Retorna True se carregou; False se deu timeout (alguns filhos ficam
    vazios legitimamente — ex.: SubClasseId sem subtipos definidos).
    Nao levanta excecao.
    """
    # Snapshot inicial
    inicial = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const reais = Array.from(el.options).filter(o => o.value);
            return { len: el.options.length, real: reais.length, first: reais[0]?.text || '' };
        }""",
        child_sel,
    )
    if inicial is None:
        return False

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        cur = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const reais = Array.from(el.options).filter(o => o.value);
                return { len: el.options.length, real: reais.length, first: reais[0]?.text || '' };
            }""",
            child_sel,
        )
        if cur is None:
            return False
        if (cur["len"] != inicial["len"]
                or cur["first"] != inicial["first"]
                or (inicial["real"] == 0 and cur["real"] > 0)):
            # Folga minima para o widget bootstrap-select terminar de pintar
            page.wait_for_timeout(150)
            LOG.debug("Filho '%s' populado (%d opcoes)", child_sel, cur["real"])
            return True
        page.wait_for_timeout(80)

    LOG.debug("Filho '%s' nao mudou em %d ms — prosseguindo", child_sel, timeout_ms)
    return False


def _aguardar_apos_select(page: Page, campo_id: str) -> None:
    """Estrategia de espera apos mudar um <select>, baseada no campo pai.

    - Campos com cascata conhecida: polling no filho
    - GrupoClienteId (cascata pesada): networkidle
    - Demais: sem espera adicional (apenas o change event basta)
    """
    if campo_id in _NETWORKIDLE:
        LOG.debug("Campo '%s': aguardando networkidle (cascata pesada)...", campo_id)
        try:
            page.wait_for_load_state("networkidle", timeout=_TEMPO_ESPERA[campo_id])
        except Exception:
            pass
        page.wait_for_timeout(200)

    elif campo_id in _DEPENDENCIAS:
        child_sel = _DEPENDENCIAS[campo_id]
        timeout   = _TEMPO_ESPERA.get(campo_id, _TEMPO_ESPERA["_default"])
        LOG.debug("Campo '%s': polling filho '%s' (max %d ms)...", campo_id, child_sel, timeout)
        _esperar_filho_popular(page, child_sel, timeout)

    else:
        # Combo sem dependente conhecido — sem espera adicional
        LOG.debug("Campo '%s': sem cascata conhecida, sem espera extra.", campo_id)


def _abrir_edicao(page: Page) -> None:
    """Navega para /processo/edit/<id> lendo o href do link Editar no DOM.

    ATENCAO: open_pasta() navega com wait_until='commit' (apenas headers HTTP).
    O DOM da pagina de detalhes pode ainda estar carregando quando chegamos aqui.
    Por isso aguardamos o link de edicao aparecer antes de fazer o querySelector,
    em vez de correr o risco de ler um DOM vazio.
    """
    # Aguarda o link de edicao aparecer no DOM (pode estar a carregar ainda)
    try:
        page.wait_for_selector(
            "a[href*='/processo/edit/']",
            state="attached",
            timeout=20_000,
        )
    except PlaywrightTimeoutError:
        pass  # se nao aparecer em 20s o evaluate abaixo vai lancar o erro descritivo

    edit_href = page.evaluate(
        """() => {
            const l = document.querySelector("a[href*='/processo/edit/']");
            return l ? l.getAttribute('href') : null;
        }"""
    )
    if not edit_href:
        raise RuntimeError(
            "Link 'Editar' nao encontrado na pagina de detalhes. "
            "Verifique se a pasta esta aberta e se tem permissao de edicao."
        )
    url = (ELAW_URL + edit_href) if edit_href.startswith("/") else edit_href
    LOG.info("Abrindo edicao: %s", url)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(SEL_BTN_SALVAR, timeout=20_000)
    LOG.info("Formulario de edicao pronto.")


def _tentar_selecionar_autocomplete(page: Page, timeout_ms: int = 3_000) -> bool:
    """Aguarda dropdown de autocomplete aparecer e clica na primeira sugestao.

    O ELAW usa campos 'ValorPreenchido' (texto visivel) + campo oculto de ID.
    Sem selecionar a sugestao do dropdown, o campo oculto fica vazio e o
    formulario recusa o salvamento silenciosamente (causa do timeout).

    Retorna True se selecionou, False se nao havia dropdown.
    """
    # Seletores em ordem de probabilidade para o ELAW (jQuery UI, Devbridge, generico)
    _SELETORES_AC = [
        ".ui-autocomplete .ui-menu-item:first-child",
        ".ui-autocomplete .ui-menu-item:first-child a",
        ".autocomplete-suggestions .autocomplete-suggestion:first-child",
        ".tt-suggestions .tt-suggestion:first-child",
        "[class*='autocomplete'] li:first-child",
    ]
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in _SELETORES_AC:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    texto = loc.first.inner_text(timeout=1_000).strip()
                    loc.first.click(timeout=2_000)
                    LOG.info("Autocomplete: sugestao selecionada ('%s')", texto[:60])
                    return True
            except Exception:
                pass
        page.wait_for_timeout(100)
    LOG.debug("Autocomplete: nenhum dropdown detectado em %d ms", timeout_ms)
    return False


def _preencher_campo(page: Page, nome: str, valor: str) -> None:
    """Localiza o campo pelo label e preenche com o valor.

    Detecta automaticamente se e select (combolist) ou input/textarea
    e aplica a estrategia correta para cada tipo.
    """
    sel = _field_selector(nome)
    loc = page.locator(sel).first

    # Coleta tag E id do elemento em uma unica chamada JS
    try:
        info = loc.evaluate(
            "el => ({ tag: el.tagName.toLowerCase(), id: el.id || '', name: el.name || '' })",
            timeout=10_000,
        )
    except Exception as e:
        raise RuntimeError(
            f"Campo '{nome}' nao encontrado no formulario. "
            f"Verifique se o nome do label e exatamente como aparece no sistema."
        ) from e

    tag      = info["tag"]
    campo_id = info["id"] or info["name"]
    LOG.info("Campo '%s' → <%s id='%s'>", nome, tag, campo_id)

    if tag == "select":
        # Tenta pelo label visivel; fallback pelo atributo value
        try:
            loc.select_option(label=valor)
        except Exception:
            try:
                loc.select_option(value=valor)
            except Exception as e:
                raise RuntimeError(
                    f"Campo '{nome}': opcao '{valor}' nao encontrada. "
                    f"Verifique se o valor e exatamente como aparece no sistema."
                ) from e

        # Dispara eventos e atualiza widgets jQuery (bootstrap-select / select2)
        page.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('change', {bubbles: true}));
                if (window.jQuery) {
                    try { window.jQuery(el).trigger('change'); }           catch(e) {}
                    try { window.jQuery(el).trigger('change.select2'); }   catch(e) {}
                    try {
                        if (window.jQuery.fn.selectpicker)
                            window.jQuery(el).selectpicker('refresh');
                    } catch(e) {}
                    try {
                        if (window.jQuery.fn.multiselect)
                            window.jQuery(el).multiselect('refresh');
                    } catch(e) {}
                }
            }""",
            loc.element_handle(),
        )

        # Espera inteligente baseada no campo pai (polling / networkidle / nada)
        _aguardar_apos_select(page, campo_id)

    else:
        # input texto ou textarea
        loc.click(timeout=5_000)
        loc.fill(valor)
        # Dispara input/keyup para ativar autocomplete — SEM blur ainda
        # (blur fecha o dropdown antes da sugestao ser selecionada)
        page.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('input',       {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'a'}));
            }""",
            loc.element_handle(),
        )
        # Tenta selecionar sugestao de autocomplete se aparecer
        _tentar_selecionar_autocomplete(page)
        # Agora sim: change + blur para notificar frameworks de validacao
        page.evaluate(
            """(el) => {
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur',   {bubbles: true}));
            }""",
            loc.element_handle(),
        )
        page.wait_for_timeout(150)

    LOG.info("Campo '%s' preenchido com '%s'", nome, valor)


def _clicar_salvar(page: Page) -> None:
    """Clica em Salvar via JS — evita scroll/blur interferindo."""
    clicou = page.evaluate(
        """() => {
            const btn = document.getElementById('buttonSave');
            if (btn) { btn.click(); return true; }
            const fb = [...document.querySelectorAll('a.btn, button.btn')]
                .find(el => el.textContent.trim() === 'Salvar' ||
                            el.classList.contains('buttonSave'));
            if (fb) { fb.click(); return 'fallback'; }
            return false;
        }"""
    )
    if not clicou:
        raise RuntimeError("Botao Salvar (#buttonSave) nao encontrado.")
    LOG.info("Salvar clicado (%s).", clicou)


# JS que varre o DOM apos um save bloqueado e devolve os campos e mensagens
# que o ELAW apontou. Mapeia cada campo invalido de volta para o TEXTO DO LABEL
# (o que a coordenadora ve no formulario) e junta as mensagens do pop-up/toast.
# ELAW = ASP.NET MVC (validation-summary/field-validation) + toastr (#toast-container).
_JS_COLETAR_ERROS = r"""
() => {
    const out = { campos: [], mensagens: [] };
    const vistosC = new Set(), vistosM = new Set();

    const visivel = (el) => !!el && (el.offsetParent !== null
        || (el.getClientRects && el.getClientRects().length > 0));

    const limpaLabel = (t) => (t || '')
        .replace(/[\* ]/g, ' ')
        .replace(/\s+/g, ' ')
        .replace(/\s*:\s*$/, '')
        .trim();

    const addCampo = (t) => {
        const v = limpaLabel(t);
        if (v && v.length <= 80 && !vistosC.has(v.toLowerCase())) {
            vistosC.add(v.toLowerCase());
            out.campos.push(v);
        }
    };
    const addMsg = (t) => {
        const v = (t || '').replace(/\s+/g, ' ').trim();
        if (v && !vistosM.has(v.toLowerCase())) {
            vistosM.add(v.toLowerCase());
            out.mensagens.push(v.slice(0, 200));
        }
    };

    const labelDoCampo = (el) => {
        const fg = el.closest('.form-group, .form-line');
        if (fg) {
            const lb = fg.querySelector('label');
            if (lb && lb.innerText.trim()) return lb.innerText;
        }
        if (el.id) {
            const lb = document.querySelector('label[for="' + el.id + '"]');
            if (lb && lb.innerText.trim()) return lb.innerText;
        }
        return el.getAttribute('aria-label') || el.getAttribute('placeholder')
            || el.name || el.id || '';
    };

    // A) Toasts (toastr) — todas as mensagens de erro/aviso
    document.querySelectorAll(
        '#toast-container .toast-error, #toast-container .toast-warning'
    ).forEach(t => {
        if (!visivel(t)) return;
        const m = t.querySelector('.toast-message') || t;
        addMsg(m.innerText);
    });

    // B) Resumo de validacao (ASP.NET MVC) e alertas genericos
    document.querySelectorAll('.validation-summary-errors li').forEach(li => addMsg(li.innerText));
    document.querySelectorAll('.alert-danger, .message-error, .error-message').forEach(a => {
        if (visivel(a)) addMsg(a.innerText);
    });

    // C) Campos invalidos -> label visivel no formulario
    document.querySelectorAll('.input-validation-error, [aria-invalid="true"]')
        .forEach(el => addCampo(labelDoCampo(el)));
    document.querySelectorAll('.has-error').forEach(fg => {
        const lb = fg.querySelector('label');
        if (lb) addCampo(lb.innerText);
    });

    // D) Spans de validacao inline — data-valmsg-for aponta o campo
    document.querySelectorAll('.field-validation-error').forEach(sp => {
        if (!visivel(sp)) return;
        const forName = sp.getAttribute('data-valmsg-for');
        let mapeou = false;
        if (forName) {
            const campo = document.querySelector('[name="' + forName + '"]');
            if (campo) { addCampo(labelDoCampo(campo)); mapeou = true; }
        }
        if (!mapeou) addMsg(sp.innerText);
    });

    // E) Modais / dialogos / growl — caso o pop-up seja central, nao um toast
    [
        '.swal2-container .swal2-html-container', '.swal2-container .swal2-title',
        '.sweet-alert p', '.sweet-alert .lead',
        '.modal.in .modal-body', '.modal.show .modal-body',
        '.ui-dialog .ui-dialog-content',
        '.ui-messages-error-detail', '.ui-growl-message',
    ].forEach(s => {
        document.querySelectorAll(s).forEach(el => { if (visivel(el)) addMsg(el.innerText); });
    });

    return out;
}
"""


def _coletar_erros_validacao(page: Page) -> dict:
    """Le no DOM os campos e mensagens que o ELAW apontou ao bloquear o save.

    Retorna {'campos': [...], 'mensagens': [...]}. Nunca levanta excecao —
    se nao conseguir ler, devolve listas vazias.
    """
    try:
        dados = page.evaluate(_JS_COLETAR_ERROS)
        return {
            "campos": list(dados.get("campos") or []),
            "mensagens": list(dados.get("mensagens") or []),
        }
    except Exception as e:
        LOG.debug("Nao consegui coletar os erros de validacao: %s", e)
        return {"campos": [], "mensagens": []}


# Padroes das mensagens de validacao do ELAW para extrair o NOME do campo.
# Observado em 2026-06-22:
#   "...Preencha o campo Tipo Ação."  /  "...Preencha o campo Objeto ."
#   "Value cannot be null. Parameter name: Preencha os campos corretamente ! Sub Área - Digital"
_RE_PREENCHA = re.compile(
    r"preencha\s+o\s+campo\s+(.+?)\s*(?:[.;:!]|$)",
    re.IGNORECASE,
)
# Variante "...corretamente ! <Campo>" — o nome do campo vem depois do "!"
_RE_CORRETAMENTE = re.compile(
    r"corretamente\s*!\s*(.+?)\s*$",
    re.IGNORECASE,
)
_RE_OBRIG = re.compile(
    r"(?:o\s+)?campo\s+(.+?)\s+(?:é|e'|e)\s+obrigat",
    re.IGNORECASE,
)
_RE_GENERICO = re.compile(
    r"verifique se todos os campos.*?corretamente\s*",
    re.IGNORECASE,
)
# Prefixo tecnico do ASP.NET as vezes vem grudado ("Value cannot be null. Parameter name: ...")
_RE_PREFIXO_TECNICO = re.compile(r"^.*?parameter name:\s*", re.IGNORECASE)


def _limpa_nome_campo(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip().strip("\"'").strip()
    # nomes de campo sao curtos; se veio uma frase longa, descarta
    return s if s and len(s) <= 50 else ""


def _extrair_campos_de_msg(msg: str) -> list[str]:
    """Extrai nomes de campos de uma mensagem do ELAW (ex.: 'Preencha o campo X')."""
    achados: list[str] = []
    for rx in (_RE_PREENCHA, _RE_CORRETAMENTE, _RE_OBRIG):
        for g in rx.findall(msg or ""):
            nome = _limpa_nome_campo(g)
            if nome and nome.lower() not in (a.lower() for a in achados):
                achados.append(nome)
    return achados


def _juntar_natural(itens: list[str]) -> str:
    if len(itens) == 1:
        return itens[0]
    return ", ".join(itens[:-1]) + " e " + itens[-1]


def _montar_msg_save_bloqueado(dados: dict) -> str:
    """Monta uma mensagem curta de UMA linha: 'Campos obrigatorios: X e Y.'

    Junta os campos lidos do DOM com os nomes extraidos das mensagens do ELAW
    ('Preencha o campo X'). O _friendly do server/main usa so a 1a linha.
    """
    MAX_CAMPOS = 15
    campos: list[str] = list(dados.get("campos") or [])
    mensagens: list[str] = list(dados.get("mensagens") or [])

    # Extrai nomes de campos das mensagens (fonte principal no ELAW real)
    for m in mensagens:
        for nome in _extrair_campos_de_msg(m):
            if nome.lower() not in (c.lower() for c in campos):
                campos.append(nome)

    if campos:
        extra = ""
        if len(campos) > MAX_CAMPOS:
            extra = " (+%d)" % (len(campos) - MAX_CAMPOS)
            campos = campos[:MAX_CAMPOS]
        rotulo = "Campo obrigatório" if len(campos) == 1 else "Campos obrigatórios"
        return "%s: %s%s" % (rotulo, _juntar_natural(campos), extra)

    # Nao deu para extrair campos — mostra a mensagem do ELAW sem o cabecalho generico
    limpas: list[str] = []
    for m in mensagens:
        m2 = _RE_GENERICO.sub("", m)
        m2 = _RE_PREFIXO_TECNICO.sub("", m2).strip()
        if m2 and m2.lower() not in (x.lower() for x in limpas):
            limpas.append(m2)
    if limpas:
        return "Nao foi possivel salvar — " + " | ".join(limpas[:3])

    return ("Nao foi possivel salvar: ha campo(s) obrigatorio(s) pendente(s) "
            "que o robo nao edita, mas nao consegui ler quais no pop-up. "
            "Abra a pasta no ELAW para ver os campos destacados em vermelho.")


def _dump_html_erro(page: Page) -> None:
    """Salva o HTML da pagina quando o save falhou mas nada foi capturado,
    para permitir refinar os seletores do pop-up depois. Best-effort —
    nunca interrompe o fluxo."""
    try:
        from pathlib import Path
        d = Path(__file__).resolve().parent.parent / "logs"
        d.mkdir(exist_ok=True)
        (d / "save_erro_ultimo.html").write_text(page.content(), encoding="utf-8")
        LOG.warning("HTML do erro salvo em logs/save_erro_ultimo.html para diagnostico")
    except Exception as e:
        LOG.debug("Nao consegui salvar o HTML de diagnostico: %s", e)


def _titulo_seguro(page: Page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return "?"


def _eh_sucesso(page: Page) -> bool:
    """True se o save concluiu. O ELAW renderiza a tela de 'Detalhes do Processo'
    na resposta do save — as vezes SEM mudar a URL de /processo/edit/ —, entao
    nao basta olhar a URL: o titulo da pagina e' o sinal confiavel."""
    try:
        u = page.url.lower()
        if "/processo/details" in u or "/processo/list" in u:
            return True
        return _titulo_seguro(page).strip().lower().startswith("detalhes do processo")
    except Exception:
        return False


# Decide o desfecho do save lendo o DOM: 'ok' (salvou) | 'erro' (validacao) | '' (processando).
# Sucesso e' detectado pelo titulo "Detalhes do Processo" porque o ELAW costuma
# renderizar os detalhes na resposta do save sem mudar a URL para /details/.
_JS_DESFECHO = r"""
() => {
    const url = location.href.toLowerCase();
    const titulo = (document.title || '').trim().toLowerCase();
    if (url.includes('/processo/details') || url.includes('/processo/list')
        || titulo.indexOf('detalhes do processo') === 0) return 'ok';

    if (document.querySelector('.input-validation-error')) return 'erro';
    const sels = ['#toast-container .toast-error', '.validation-summary-errors li',
                  '.alert-danger', '.field-validation-error',
                  '.swal2-container .swal2-html-container',
                  '.modal.in .modal-body', '.modal.show .modal-body',
                  '.ui-dialog .ui-dialog-content'];
    for (const s of sels) {
        for (const el of document.querySelectorAll(s)) {
            if ((el.innerText || '').trim()
                && (el.offsetParent !== null
                    || (el.getClientRects && el.getClientRects().length)))
                return 'erro';
        }
    }
    return '';
}
"""


def _aguardar_confirmacao(page: Page, timeout_ms: int = 30_000) -> None:
    """Aguarda o desfecho do save: sucesso (tela de Detalhes) OU erro de validacao.

    Orientada a eventos: sai assim que o ELAW mostra os Detalhes (salvou) ou pinta
    um erro de validacao. So usa o timeout inteiro quando o ELAW fica sem dar sinal.
    """
    desfecho = ""
    try:
        h = page.wait_for_function(_JS_DESFECHO, timeout=timeout_ms)
        desfecho = h.json_value() or ""
    except PlaywrightTimeoutError:
        desfecho = ""

    if desfecho == "ok" or _eh_sucesso(page):
        LOG.info("Salvo. URL=%s | titulo=%s", page.url, _titulo_seguro(page))
        return

    if desfecho == "erro":
        dados = _coletar_erros_validacao(page)
        LOG.warning("Save bloqueado. Campos: %s | Mensagens: %s",
                    dados["campos"], dados["mensagens"])
        raise RuntimeError(_montar_msg_save_bloqueado(dados))

    # Timeout sem desfecho claro — pode ter surgido erro tardio ou salvo tardio.
    dados = _coletar_erros_validacao(page)
    if dados["campos"] or dados["mensagens"]:
        LOG.warning("Save bloqueado (tardio). Campos: %s | Mensagens: %s",
                    dados["campos"], dados["mensagens"])
        raise RuntimeError(_montar_msg_save_bloqueado(dados))
    if _eh_sucesso(page):
        LOG.info("Salvo (confirmado tardio). URL=%s | titulo=%s", page.url, _titulo_seguro(page))
        return

    # Nem sucesso nem erro legivel: guarda o HTML e reporta de forma honesta.
    _dump_html_erro(page)
    LOG.warning("Save sem confirmacao. URL=%s | titulo=%s", page.url, _titulo_seguro(page))
    raise RuntimeError(
        "Nao foi possivel confirmar o salvamento: o ELAW nao mostrou os detalhes "
        "nem erro (possivel lentidao do sistema). Confira a pasta no ELAW."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(page: Page, config: dict, row: dict) -> str:
    """Executa o ajuste de capa para um ID. Retorna mensagem de detalhe."""

    campos = config.get("campos", [])
    if not campos:
        raise RuntimeError("Nenhum campo configurado para edicao")

    _abrir_edicao(page)

    aplicados: list[str] = []
    for idx, campo in enumerate(campos, 1):
        nome = (campo.get("nome") or "").strip()
        if not nome:
            raise RuntimeError(f"Campo {idx} sem nome configurado")

        if campo.get("value"):
            valor = str(campo["value"]).strip()
        elif campo.get("coluna"):
            col = str(campo["coluna"]).strip().upper()
            valor = str(row.get(col, "")).strip()
            if not valor:
                LOG.warning("Coluna %s vazia para '%s' — pulando", col, nome)
                continue
        else:
            raise RuntimeError(f"Campo '{nome}' sem value nem coluna configurados")

        _preencher_campo(page, nome, valor)
        aplicados.append(f"{nome}={valor!r}")

        # Pausa minima entre campos (AJAX dependente ja foi tratado dentro de _preencher_campo)
        if idx < len(campos):
            page.wait_for_timeout(500)

    if not aplicados:
        return "Nenhum campo foi preenchido (colunas vazias na planilha?)"

    # Pre-save: aguarda jQuery.active == 0 (sem AJAX pendente).
    # Mais confiavel que networkidle — ELAW tem polling de fundo constante
    # que impede networkidle de resolver. jQuery.active e' o contador interno
    # do proprio ELAW para saber se ha requisicoes em curso.
    LOG.info("Aguardando AJAX pendente terminar (jQuery.active)...")
    _aguardar_jquery_idle(page, timeout_ms=5_000)
    # Removido wait_for_timeout(800) fixo — jQuery.active == 0 ja garante estabilidade
    LOG.info("Formulario estavel — clicando em Salvar.")

    _clicar_salvar(page)
    _aguardar_confirmacao(page, timeout_ms=30_000)

    return f"Capa atualizada: {', '.join(aplicados)}"
