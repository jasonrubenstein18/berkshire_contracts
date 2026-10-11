"""Gradio app: executive overview, grounded contract Q&A, contract explorer, data quality and evaluation."""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr  # noqa: E402
import pandas as pd  # noqa: E402

from berkshire_contracts import app_support as ui  # noqa: E402
from berkshire_contracts.chat.service import AssistantResponse, ContractAssistant  # noqa: E402
from berkshire_contracts.config import get_settings  # noqa: E402
from berkshire_contracts.db import init_db  # noqa: E402
from berkshire_contracts.extraction.fields import ENUM_LABELS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

settings = get_settings()
init_db()
assistant = ContractAssistant()

EXAMPLES = [
    "Which contracts have auto-renewal clauses?",
    "Which contracts expire in 2026? Show a timeline.",
    "Total stated award value by vendor",
    "How many documents of each type are in the sample?",
    "What does the Motorola site lease say about renewal and notice?",
    "Which agreements cap the vendor's liability, and at what amount?",
    "For agreements the County can terminate for convenience, how much notice is required?",
    "What did Modification 3 to agreement 21164 add, and what did it cost?",
]

CSS = """
.kpi-grid {display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin:4px 0 8px;}
.kpi {background:#f5f8fc; border:1px solid #dbe4ef; border-radius:10px; padding:14px 16px;}
.kpi-value {font-size:26px; font-weight:700; color:#1f3a5f; line-height:1.1;}
.kpi-label {font-size:12px; color:#5b6b7f; margin-top:4px; text-transform:uppercase; letter-spacing:.04em;}
.disclaimer {font-size:12px; color:#5b6b7f;}
.source-box {max-height:420px; overflow-y:auto;}
"""

THEME = gr.themes.Soft(primary_hue="blue", neutral_hue="slate")


def _warm_up() -> None:
    try:
        from berkshire_contracts.retrieval.index import load_index

        load_index().encoder.encode_query("warm up")
        log.info("retrieval index and encoder ready")
    except Exception:
        log.exception("index warm-up failed; search will retry on first question")


threading.Thread(target=_warm_up, daemon=True).start()


def render_answer(r: AssistantResponse) -> str:
    route = {
        "structured": "Answered from the extracted contract database",
        "clause_search": "Answered from contract text (late-interaction search)",
        "hybrid": "Filtered the database, then read matching clauses",
        "out_of_scope": "Outside these contracts",
        "rejected": "",
    }.get(r.route, r.route)
    parts = [r.answer]
    if r.notes:
        parts.append("\n".join(f"> {n}" for n in dict.fromkeys(r.notes)))
    if r.follow_ups:
        parts.append("**Try next:** " + " · ".join(f"_{q}_" for q in r.follow_ups))
    meta = f"{route} · {r.latency_ms / 1000:.1f}s" if route else ""
    if meta:
        parts.append(f"<sub>{meta}</sub>")
    return "\n\n".join(p for p in parts if p)


def ordered_sources(r: AssistantResponse) -> list:
    cited = set(r.cited)
    return sorted(r.sources, key=lambda s: s.sid not in cited)


def render_sources(r: AssistantResponse) -> str:
    if not r.sources:
        return "_No source passages for this answer._"
    cited = set(r.cited)
    lines = []
    for s in ordered_sources(r):
        snippet = " ".join(s.text.split())
        snippet = snippet[:420] + ("…" if len(snippet) > 420 else "")
        section = f" · {s.section}" if s.section else ""
        tag = " · **cited in answer**" if s.sid in cited else ""
        lines.append(f"**[{s.sid}] {s.filename}** · page {s.page}{section}{tag}\n\n> {snippet}")
    return "\n\n".join(lines)


def _source_choice(s) -> str:
    return f"{s.sid} · p.{s.page} · {s.filename}"


def on_ask(message: str, history: list, session_id: str):
    message = (message or "").strip()
    if not message:
        return (gr.skip(),) * 9
    response = assistant.ask(message, session=session_id or "anonymous")
    history = [
        *(history or []),
        {"role": "user", "content": message},
        {"role": "assistant", "content": render_answer(response)},
    ]
    ordered = ordered_sources(response)
    sources = [s.__dict__ for s in ordered]
    choices = [_source_choice(s) for s in ordered]
    image = None
    if ordered:
        first = ordered[0]
        image = ui.render_page(first.filename, first.page, " ".join(first.text.split()[:40]))
    has_table = response.table is not None and len(response.table) > 0
    csv_path = None
    if has_table:
        with tempfile.NamedTemporaryFile(prefix="answer_", suffix=".csv", delete=False, mode="w") as handle:
            response.table.to_csv(handle, index=False)
            csv_path = handle.name
    return (
        "",
        history,
        render_sources(response),
        gr.update(value=response.table if has_table else pd.DataFrame(), visible=has_table),
        gr.update(value=response.chart, visible=response.chart is not None),
        gr.update(value=csv_path, visible=csv_path is not None),
        gr.update(choices=choices, value=choices[0] if choices else None, visible=bool(choices)),
        gr.update(value=image, visible=image is not None),
        sources,
    )


def on_source_change(choice: str | None, sources: list[dict]):
    if not choice or not sources:
        return gr.update(value=None, visible=False)
    sid = choice.split(" · ")[0]
    src = next((s for s in sources if s["sid"] == sid), None)
    if src is None:
        return gr.update(value=None, visible=False)
    image = ui.render_page(src["filename"], src["page"], " ".join(src["text"].split()[:40]))
    return gr.update(value=image, visible=image is not None)


def on_filter(doc_types, vendor, auto, review, text):
    table = ui.explorer_table(doc_types, vendor or "", auto, bool(review), text or "")
    return table, f"{len(table)} files"


def on_select_contract(evt: gr.SelectData, table: pd.DataFrame):
    try:
        row = evt.index[0] if isinstance(evt.index, list | tuple) else evt.index
        file_id = table.iloc[row]["File ID"]
    except Exception:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
    header, fields, pages = ui.contract_detail(file_id)
    filename = table.iloc[row]["File"]
    choices = [f"{name}" for name, _ in pages]
    first_page = pages[0][1] if pages else 1
    image = ui.render_page(filename, first_page, ui.evidence_quote(file_id, first_page))
    return (
        header,
        fields,
        gr.update(choices=choices, value=choices[0] if choices else None),
        gr.update(value=image, visible=image is not None),
        gr.update(value=ui.pdf_path(filename), visible=ui.pdf_path(filename) is not None),
        {"file_id": file_id, "filename": filename, "pages": pages},
    )


def on_evidence_page(choice: str | None, current: dict):
    if not choice or not current:
        return gr.skip()
    page = dict(current["pages"]).get(choice)
    if page is None:
        return gr.skip()
    image = ui.render_page(current["filename"], page, ui.evidence_quote(current["file_id"], page))
    return gr.update(value=image, visible=image is not None)


def _eval_sections() -> tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary = ui.load_eval_summary()
    if not summary:
        pending = pd.DataFrame({"Status": ["Not run yet: `contracts evaluate`"]})
        return "_Evaluation results not generated yet (`contracts evaluate`)._", pending, pending, pending
    md = summary.get("headline_markdown", "")
    return (
        md,
        pd.DataFrame(summary.get("extraction_by_field", [])),
        pd.DataFrame(summary.get("retrieval", [])),
        pd.DataFrame(summary.get("chat_cases", [])),
    )


def export_paths() -> dict[str, str | None]:
    exports = settings.exports_dir
    files = {
        "csv": exports / "contracts.csv",
        "xlsx": exports / "contracts.xlsx",
        "evidence": exports / "field_evidence.csv",
        "db": settings.db_path,
    }
    return {k: str(v) if v.exists() else None for k, v in files.items()}


with gr.Blocks(title="Contract Intelligence · Lake County", fill_width=True) as demo:
    session_id = gr.State("")
    sources_state = gr.State([])
    contract_state = gr.State({})

    gr.Markdown(
        "## Contract Intelligence PoC\n"
        "389 Lake County procurement documents → 19 evidence-backed fields per document, "
        "searchable with late-interaction (ColBERT) retrieval. Every value links to the page it came from."
    )

    with gr.Tabs():
        with gr.Tab("Executive overview"):
            kpi_box = gr.HTML()
            with gr.Row():
                fig_types = gr.Plot(show_label=False)
                fig_renewal = gr.Plot(show_label=False)
            with gr.Row():
                fig_exp = gr.Plot(show_label=False)
                fig_vendors = gr.Plot(show_label=False)
            with gr.Row():
                fig_risk = gr.Plot(show_label=False)
                fig_coverage = gr.Plot(show_label=False)
            gr.Markdown("#### Upcoming term ends (next 18 months) and the date to act on notice requirements")
            renewals = gr.Dataframe(interactive=False, wrap=True, max_height=360)

        with gr.Tab("Ask the contracts"):
            with gr.Row(equal_height=False):
                with gr.Column(scale=3):
                    chat = gr.Chatbot(
                        height=520,
                        show_label=False,
                        render_markdown=True,
                        placeholder="Ask about renewals, expirations, values, or specific clauses.",
                    )
                    with gr.Row():
                        msg = gr.Textbox(
                            placeholder="e.g. Which contracts auto-renew, and what notice is required?",
                            show_label=False,
                            scale=8,
                            max_length=settings.max_question_chars,
                            autofocus=True,
                        )
                        send = gr.Button("Ask", variant="primary", scale=1)
                    with gr.Row():
                        example_buttons = [gr.Button(q, size="sm") for q in EXAMPLES]
                    result_table = gr.Dataframe(
                        visible=False,
                        interactive=False,
                        wrap=True,
                        max_height=380,
                        label="Matching documents",
                    )
                    result_chart = gr.Plot(visible=False, show_label=False)
                    result_csv = gr.DownloadButton("Download these results (CSV)", visible=False, size="sm")
                with gr.Column(scale=2):
                    gr.Markdown("#### Sources")
                    sources_md = gr.Markdown(
                        "_Ask a question to see the passages behind the answer._", elem_classes=["source-box"]
                    )
                    source_pick = gr.Dropdown(label="Preview source page", visible=False, interactive=True)
                    source_image = gr.Image(
                        visible=False, show_label=False, type="pil", height=640, interactive=False
                    )
            gr.Markdown(
                "<div class='disclaimer'>Answers cite numbered passages from the documents; when the passages "
                "don't support an answer the assistant says so instead of guessing. Structured answers come from "
                "the extracted database and list the evidence quote for each row.</div>"
            )

        with gr.Tab("Contract explorer"):
            with gr.Row():
                f_types = gr.Dropdown(
                    sorted(
                        set(
                            ENUM_LABELS[k]
                            for k in (
                                "agreement",
                                "amendment_modification",
                                "renewal_extension",
                                "price_adjustment",
                                "award_notice",
                                "task_order_sow",
                                "vendor_disclosure",
                                "solicitation_bid",
                                "lease",
                                "other",
                            )
                        )
                    ),
                    multiselect=True,
                    label="Document type",
                )
                f_vendor = gr.Textbox(label="Vendor contains")
                f_text = gr.Textbox(label="File / contract # / description contains")
                f_auto = gr.Radio(["Any", "Yes", "No"], value="Any", label="Auto-renews")
                f_review = gr.Checkbox(label="Only rows flagged for review")
            count_md = gr.Markdown()
            explorer = gr.Dataframe(
                interactive=False, wrap=False, max_height=420, show_search="search", pinned_columns=1
            )
            gr.Markdown("_Select a row to see every field with its evidence quote and source page._")
            with gr.Row(equal_height=False):
                with gr.Column(scale=3):
                    detail_md = gr.Markdown()
                    detail_fields = gr.Dataframe(interactive=False, wrap=True, max_height=620)
                with gr.Column(scale=2):
                    evidence_pick = gr.Dropdown(label="Evidence page", interactive=True)
                    evidence_image = gr.Image(
                        visible=False, show_label=False, type="pil", height=680, interactive=False
                    )
                    pdf_download = gr.DownloadButton("Download original PDF", visible=False, size="sm")

        with gr.Tab("Data quality & evaluation"):
            gr.Markdown("#### Parsing and OCR")
            with gr.Row():
                q_parse = gr.Dataframe(interactive=False)
                q_status = gr.Dataframe(interactive=False)
                q_reasons = gr.Dataframe(interactive=False)
            gr.Markdown("#### Extraction coverage and evidence verification by field")
            q_fields = gr.Dataframe(interactive=False)
            gr.Markdown(
                "#### Evaluation against labeled ground truth (AI-labeled from the source text; human audit pending)"
            )
            eval_md = gr.Markdown()
            eval_fields = gr.Dataframe(interactive=False, label="Field accuracy")
            eval_retrieval = gr.Dataframe(interactive=False, label="Retrieval: late interaction vs BM25")
            eval_chat = gr.Dataframe(interactive=False, wrap=True, label="Chat test cases")
            gr.Markdown("#### Model usage and cost")
            q_cost = gr.Dataframe(interactive=False)
            gr.Markdown("#### Downloads")
            with gr.Row():
                dl_csv = gr.DownloadButton("Contracts table (CSV, one row per file)", size="sm")
                dl_xlsx = gr.DownloadButton("Contracts workbook (XLSX + data dictionary)", size="sm")
                dl_evidence = gr.DownloadButton("Field evidence (CSV)", size="sm")
                dl_db = gr.DownloadButton("SQLite database", size="sm")

    def load_all():
        df = ui.load_contracts_frame()
        figs = ui.overview_figures(df)
        quality = ui.quality_tables()
        e_md, e_fields, e_ret, e_chat = _eval_sections()
        paths = export_paths()
        table = ui.explorer_table()
        return (
            uuid.uuid4().hex,
            ui.kpi_html(ui.kpis(df)),
            figs["types"],
            figs["renewal"],
            figs["expirations"],
            figs["vendors"],
            figs["risk"],
            figs["coverage"],
            ui.upcoming_renewals(df),
            table,
            f"{len(table)} files",
            quality["parse"],
            quality["status"],
            quality["reasons"],
            quality["fields"],
            quality["cost"],
            e_md,
            e_fields,
            e_ret,
            e_chat,
            gr.update(value=paths["csv"], visible=paths["csv"] is not None),
            gr.update(value=paths["xlsx"], visible=paths["xlsx"] is not None),
            gr.update(value=paths["evidence"], visible=paths["evidence"] is not None),
            gr.update(value=paths["db"], visible=paths["db"] is not None),
        )

    demo.load(
        load_all,
        outputs=[
            session_id,
            kpi_box,
            fig_types,
            fig_renewal,
            fig_exp,
            fig_vendors,
            fig_risk,
            fig_coverage,
            renewals,
            explorer,
            count_md,
            q_parse,
            q_status,
            q_reasons,
            q_fields,
            q_cost,
            eval_md,
            eval_fields,
            eval_retrieval,
            eval_chat,
            dl_csv,
            dl_xlsx,
            dl_evidence,
            dl_db,
        ],
    )

    ask_outputs = [
        msg,
        chat,
        sources_md,
        result_table,
        result_chart,
        result_csv,
        source_pick,
        source_image,
        sources_state,
    ]
    send.click(on_ask, [msg, chat, session_id], ask_outputs, concurrency_limit=4)
    msg.submit(on_ask, [msg, chat, session_id], ask_outputs, concurrency_limit=4)
    for button, question in zip(example_buttons, EXAMPLES, strict=True):
        button.click(lambda q=question: q, outputs=msg).then(on_ask, [msg, chat, session_id], ask_outputs)
    source_pick.change(on_source_change, [source_pick, sources_state], source_image)

    filter_inputs = [f_types, f_vendor, f_auto, f_review, f_text]
    for comp in filter_inputs:
        comp.change(on_filter, filter_inputs, [explorer, count_md])
    explorer.select(
        on_select_contract,
        [explorer],
        [detail_md, detail_fields, evidence_pick, evidence_image, pdf_download, contract_state],
    )
    evidence_pick.change(on_evidence_page, [evidence_pick, contract_state], evidence_image)


def _auth() -> tuple[str, str] | None:
    user = settings.app_username
    password = settings.app_password.get_secret_value() if settings.app_password else None
    if user and password:
        return (user, password)
    log.warning("APP_USERNAME / APP_PASSWORD not set: the app is running without a login")
    return None


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4, max_size=32)
    demo.launch(
        auth=_auth(),
        auth_message="Berkshire Partners contract intelligence demo. Use the credentials shared with you.",
        theme=THEME,
        css=CSS,
        allowed_paths=[str(settings.pdf_dir), str(settings.exports_dir), str(settings.db_path)],
        server_name=os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1"),
        show_error=False,
    )
