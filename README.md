# ELAW RPA

Automação para o sistema ELAW (`viseuadv.elawio.com.br`). Interface em janela
nativa com cinco passos, processa uma planilha de IDs e executa uma das quatro
atividades: Capa, Prazos, Pedidos ou Documentos.

## Estrutura

```
elaw_rpa/
├── main.py                    # Entry point - abre a janela e expoe a API ao JS
├── requirements.txt
├── README.md                  # este arquivo
├── automation/
│   ├── browser.py             # Login + busca por ID (Playwright)
│   ├── capa.py                # Atividade: editar campos da Capa
│   ├── prazos.py              # Atividade: ajustar Prazo Interno/Fatal
│   ├── pedidos.py             # Atividade: criar/editar Pedidos
│   └── documentos.py          # Atividade: upload/download de Documentos
├── utils/
│   ├── spreadsheet.py         # Leitura de .xlsx/.csv
│   └── reporter.py            # Geracao do relatorio final em .xlsx
└── web/
    └── elaw_rpa.html          # Interface em janela
```

## Instalação e uso (jeito fácil)

Pré-requisito: Python 3.10+ instalado com a opção **"Add Python to PATH"** marcada.

Dê **dois cliques** em `run.bat`. Na primeira vez ele instala tudo (pip + Playwright Chromium) e abre a janela. Nas execuções seguintes, só abre direto.

## Instalação manual (alternativa)

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py
# ou para Playwright invisivel:
python main.py --headless
```

A janela abre o fluxo de cinco passos:

1. **Atividade** — escolha Capa, Prazos, Pedidos ou Documentos.
2. **Planilha** — selecione o `.xlsx`/`.csv` com os números de processo (coluna A).
3. **Configuração** — específica de cada atividade.
4. **Credenciais** — usuário e senha do ELAW.
5. **Execução** — acompanhamento em tempo real + relatório `.xlsx`.

O relatório é salvo em `~/Documents/ELAW_RPA_Relatorios/`.

---

## Selectors

Todos os selectors foram confirmados via DevTools no ELAW real
(`https://viseuadv.elawio.com.br`), incluindo os toasts de sucesso e erro
(biblioteca **toastr**) capturados em um Save real.

### Confirmados

| Onde | Selector | O que é |
|---|---|---|
| `browser.py` | `LOGIN_URL = /Auth/Login` | URL da página de login |
| `browser.py` | `#Email`, `#Password`, `#btn-login` | login |
| `browser.py` | `#Filters_Protocolo` + `#buttonSubmit` | busca por número de processo |
| `browser.py` | `#processoList table tbody tr` | grid de resultados |
| `browser.py` | `a[href*='/processo/details/']` | link para abrir a pasta |
| `browser.py` | `#processoList td.dataTables_empty` | mensagem "sem resultados" |
| `capa.py` | `a[data-toggle='tab'][href='#box-dadosprincipais']` | aba Geral |
| `capa.py` | `a[href*='/processo/edit/']` | botão Editar da Capa |
| `capa.py` | `#buttonSave` | Salvar Capa |
| `prazos.py` | `a[data-toggle='tab'][href='#box-compromissos']` | aba Prazos |
| `prazos.py` | `a[href*='/compromisso/details/']` | link Detalhes na linha (não tem botão Editar inline) |
| `prazos.py` | `#buttonEditCompromisso` | botão Alterar dentro da página de detalhes |
| `prazos.py` | `#DataCompromisso` | input "Data do Prazo Interno" |
| `prazos.py` | `#DataPrazoFatalManual` | input "Prazo Fatal" |
| `prazos.py` | `button.btn-primary:has-text('Salvar')` | Salvar (botão sem id) |
| `pedidos.py` | `a[data-toggle='tab'][href='#box-pedidos']` | aba Pedidos |
| `pedidos.py` | `#buttonNewPedido` | Novo Pedido |
| `pedidos.py` | `a.buttonEditPedido` | Editar pedido (na linha) |
| `pedidos.py` | `#dialog-modal` | modal onde o form de pedido aparece |
| `pedidos.py` | `#TipoPedidoId` | select "Tipo do Pedido" |
| `pedidos.py` | `#ProvisaoId` | select "Risco" |
| `pedidos.py` | `#DataBaseCalculoId` | select "Data Base Cálculo Juros" |
| `pedidos.py` | `#Valor`, `#DataPedido`, `#ValorProvisao` | inputs do pedido |
| `pedidos.py` | `#dialog-modal button.btn-primary` | Salvar do modal |
| `documentos.py` | `a[data-toggle='tab'][href='#box-documentos']` | aba Documentos |
| `documentos.py` | `#buttonNewDocumento` | Novo Documento |
| `documentos.py` | `#FullProcessoId` | select "Fase" (= categoria do documento) |
| `documentos.py` | `#ufile` (multiple) | input de arquivo no upload |
| `documentos.py` | `a[title='Visualizar Documento']` | link de download na linha (href é `javascript:OpenUrl('/documento/download/{id}')`) |

### Toast de sucesso e erro

O ELAW usa a biblioteca **toastr**. Os selectors confirmados são:

```python
SEL_TOAST_OK    = "#toast-container .toast-success"
SEL_TOAST_ERROR = "#toast-container .toast-error"
```

Cada módulo aguarda **OK ou ERROR**. Se aparecer um `toast-error`, o
código lê o `.toast-message` e levanta `RuntimeError` com a mensagem do
ELAW (ex: "Preencha os campos corretamente: Tipo do Pedido"), que vai
parar no relatório como motivo do erro.

Caso especial — **Capa**: após salvar, o ELAW redireciona de
`/processo/edit/X` para `/processo/details?id=X` em vez de mostrar toast.
O código detecta a navegação com `page.wait_for_url("**/processo/details*")`.

---

## Tratamento de erros

A automação é fail-safe: se um processo falhar, registra o erro no relatório
e continua. Tipos tratados:

- Processo não encontrado → status `error`
- Mais de 1 resultado na busca → status `error` ("pulado por segurança")
- Timeout / elemento não encontrado → status `error`
- Linha sem ID na planilha → status `skip`

Botão **Parar** interrompe entre IDs (não cancela um que já está rodando).

## Personalização

- **Quantidade de campos da Capa**: hoje o `<select>` permite 1, 2 ou 3.
  Adicione mais opções no `<select id="numCampos">` em `web/elaw_rpa.html`.
- **Tempo limite**: ajuste `timeout_ms` ao instanciar `El