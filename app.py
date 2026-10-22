"""MILAAN — ITC gap detection for small Indian traders.

Task 1 foundation: minimal Streamlit shell with SQLite persistence.
Task 2: supplier + invoice demo-data section.
Task 3: deterministic invoice validation (validation.py) with visible
status in the UI.
Task 4: GSTR-2B CSV/XLSX staging import.
Task 5: deterministic reconciliation against one selected GSTR-2B batch.
Task 6: supplier follow-up draft preparation and approval records only.
Task 7: delivery safety and audit history. The UI permits mock-only
simulation; live WhatsApp delivery is disabled in this build.

Run: .venv/bin/streamlit run app.py
"""

import html
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
import db  # noqa: E402
import delivery  # noqa: E402
import demo_workflow  # noqa: E402
import gstr2b  # noqa: E402
import invoice_import  # noqa: E402
import reconciliation  # noqa: E402
import security  # noqa: E402
import validation  # noqa: E402

APP_CSS = """
<style>
:root {
  --ink: #142621;
  --forest: #0f6455;
  --forest-deep: #0b5044;
  --paper: #f6f7f5;
  --surface: #fcfdfb;
  --line: #d7e2dc;
  --line-strong: #c3d3cc;
  --muted: #5d7069;
  --sidebar: #072e27;
  --sidebar-text: #e8f2ed;
  --sidebar-muted: #9bb6ac;
}

html, body, .stApp, button, input, textarea, select {
  font-family: "Avenir Next", "Segoe UI", system-ui, -apple-system, sans-serif;
}

.stApp {
  background: var(--paper);
  color: var(--ink);
}

::selection {
  background: #cde8de;
  color: #102a24;
}

* {
  scrollbar-width: thin;
  scrollbar-color: #b4c9c1 transparent;
}

::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}

::-webkit-scrollbar-thumb {
  background: #b4c9c1;
  border: 3px solid var(--paper);
  border-radius: 999px;
}

a {
  color: var(--forest);
  text-underline-offset: 0.18em;
}

h1, h2, h3 {
  color: var(--ink);
  text-wrap: balance;
}

h2 {
  border-top: 1px solid var(--line);
  margin-top: 2.2rem !important;
  padding-top: 1.25rem;
  font-size: 1.38rem !important;
  font-weight: 650 !important;
  letter-spacing: -0.025em;
}

h2 [data-testid="stIconMaterial"] {
  color: var(--forest);
}

h3 {
  letter-spacing: -0.015em;
  font-weight: 650 !important;
}

p, li {
  line-height: 1.62;
}

[data-testid="stCaptionContainer"] p {
  color: var(--muted);
  font-size: 0.85rem;
  line-height: 1.55;
}

hr {
  border-color: var(--line);
  opacity: 0.7;
}

.block-container {
  max-width: 1380px;
  padding: 0.6rem 2rem 3.5rem;
}

[data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"],
[data-testid="stStatusWidget"],
[data-testid="stDecoration"],
#MainMenu {
  display: none !important;
  visibility: hidden !important;
}

header[data-testid="stHeader"] {
  height: 0;
  background: transparent;
  pointer-events: none;
}

[data-testid="stExpandSidebarButton"] {
  position: fixed;
  top: 14px;
  left: 16px;
  z-index: 1001;
  width: 40px;
  height: 40px;
  border: 1px solid #dfe5df;
  border-radius: 10px;
  background: #ffffff;
  box-shadow: 0 4px 14px rgba(16, 36, 29, 0.08);
  pointer-events: auto;
}

[data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"] {
  color: #13251f !important;
  font-size: 1.25rem;
}

@media (min-width: 861px) {
  [data-testid="stSidebarCollapseButton"] {
    display: none !important;
  }
}

section[data-testid="stSidebar"] {
  width: 264px !important;
  min-width: 264px !important;
  border-right: 0;
  background: var(--sidebar) !important;
}

section[data-testid="stSidebar"] > div {
  background: transparent !important;
}

[data-testid="stSidebarUserContent"] {
  display: flex;
  flex-direction: column;
  min-height: calc(100vh - 1.5rem);
  padding: 1.3rem 0.85rem 0.9rem;
}

section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] label {
  color: var(--sidebar-text);
}

section[data-testid="stSidebar"] .stButton > button {
  border-color: rgba(225, 244, 237, 0.2);
  background: transparent;
  color: #e8f2ed;
}

section[data-testid="stSidebar"] .stButton > button:hover {
  border-color: rgba(145, 227, 199, 0.55);
  background: rgba(255, 255, 255, 0.06);
  color: #ffffff;
}

section[data-testid="stSidebar"] [data-testid="stExpander"] {
  margin-top: 1rem;
  border-color: rgba(226, 242, 234, 0.12);
  background: rgba(255, 255, 255, 0.025);
}

section[data-testid="stSidebar"] [data-testid="stExpander"] details {
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.035) !important;
}

section[data-testid="stSidebar"] [data-testid="stExpander"] summary,
section[data-testid="stSidebar"] [data-testid="stExpander"] p {
  color: #b8ccc1;
  font-size: 0.74rem;
}

section[data-testid="stSidebar"] [data-testid="stExpander"] summary {
  background: transparent !important;
}

section[data-testid="stSidebar"] [data-testid="stExpander"] summary:hover {
  color: #ffffff;
}

section[data-testid="stSidebar"] [data-testid="stDataFrame"] {
  border-color: rgba(226, 242, 234, 0.12);
}

[data-testid="stSidebarNav"] {
  padding: 0.35rem 0.65rem 0.9rem !important;
}

[data-testid="stSidebarNav"]::before {
  content: "WORKSPACE";
  display: block;
  padding: 0.5rem 0.65rem 0.4rem;
  color: var(--sidebar-muted);
  font-size: 0.66rem;
  font-weight: 600;
  letter-spacing: 0.12em;
}

[data-testid="stSidebarNav"] ul {
  gap: 0.15rem;
}

[data-testid="stSidebarNav"] li a {
  border-radius: 9px;
  color: var(--sidebar-text) !important;
  font-size: 0.9rem;
  font-weight: 500;
  padding: 0.48rem 0.65rem !important;
}

[data-testid="stSidebarNav"] li a:hover {
  background: rgba(255, 255, 255, 0.07);
}

[data-testid="stSidebarNav"] li a[aria-current="page"] {
  background: rgba(183, 203, 177, 0.16);
  color: #ffffff !important;
  font-weight: 600;
}

[data-testid="stSidebarNav"] li a span {
  color: inherit !important;
}

.sb-footer {
  margin-top: auto;
  padding: 1rem 0.55rem 0;
  border-top: 1px solid rgba(226, 242, 234, 0.1);
  color: #d2dfd8;
  font-size: 0.74rem;
  line-height: 1.5;
}

.sb-version {
  margin-top: 0.35rem;
  color: #789287;
  font-size: 0.68rem;
}

div[data-testid="stMetric"] {
  min-height: 5.15rem;
  padding: 0.9rem 1rem;
  border: 1px solid var(--line);
  border-radius: 0.85rem;
  background: var(--surface);
  box-shadow: 0 12px 24px -24px rgba(20, 38, 33, 0.38);
}

div[data-testid="stMetricLabel"] p {
  color: var(--muted);
  font-size: 0.73rem;
  font-weight: 650;
  letter-spacing: 0.01em;
}

div[data-testid="stMetricValue"] {
  color: var(--ink);
  font-size: 1.55rem;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.035em;
}

div[data-testid="stDataFrame"] {
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 0.85rem;
  background: #ffffff;
}

[data-testid="stExpander"] details {
  border: 1px solid var(--line);
  border-radius: 0.85rem;
  background: var(--surface);
}

[data-testid="stExpander"] summary {
  font-weight: 600;
}

[data-testid="stExpander"] summary:hover {
  color: var(--forest);
}

[data-testid="stAlert"] {
  border-radius: 0.85rem;
  border: 1px solid var(--line);
}

.stButton > button,
.stDownloadButton > button {
  min-height: 2.55rem;
  border-radius: 0.6rem;
  border-color: var(--line-strong);
  color: var(--ink);
  font-weight: 650;
  letter-spacing: -0.005em;
  transition: background-color 160ms ease, border-color 160ms ease,
    color 160ms ease, box-shadow 160ms ease;
}

.stButton > button:hover,
.stDownloadButton > button:hover {
  border-color: #8fb3a6;
  background: #f2f8f5;
  color: var(--forest-deep);
}

.stButton > button[kind="primary"] {
  border-color: var(--forest);
  background: var(--forest);
  color: #ffffff;
  box-shadow: 0 10px 20px -16px rgba(15, 100, 85, 0.7);
}

.stButton > button[kind="primary"]:hover {
  border-color: var(--forest-deep);
  background: var(--forest-deep);
  color: #ffffff;
}

button:focus-visible,
a:focus-visible,
[role="button"]:focus-visible {
  outline: 2px solid #2e9d83;
  outline-offset: 2px;
}

[data-testid="stFileUploaderDropzone"] {
  padding: 1.15rem;
  border: 1px dashed #a8c3b9;
  border-radius: 0.9rem;
  background: #fbfdfb;
}

[data-testid="stFileUploaderDropzone"]:hover {
  border-color: #75a797;
  background: #f6fbf8;
}

[data-baseweb="input"],
[data-baseweb="select"] > div,
[data-baseweb="textarea"] {
  border-radius: 0.6rem !important;
}

button[data-baseweb="tab"] {
  color: var(--muted);
  font-weight: 600;
}

button[data-baseweb="tab"][aria-selected="true"] {
  color: var(--forest);
}

[data-baseweb="tab-highlight"] {
  background: var(--forest);
}

[data-baseweb="tab-border"] {
  background: var(--line);
}

code {
  color: #275f52;
  background: #eaf3ee;
  border-radius: 0.3rem;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1.25rem;
  margin-bottom: 1.35rem;
  padding: 0.95rem 1.2rem;
  border: 1px solid #e3e8e2;
  border-radius: 12px;
  background: #ffffff;
}

.workspace-name {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  color: #15271f;
  font-size: 0.95rem;
  font-weight: 650;
}

.workspace-name svg {
  color: #78867f;
}

.workspace-gstin {
  margin-top: 0.18rem;
  color: #7d8b84;
  font-size: 0.74rem;
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 0.7rem;
}

.period-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.52rem 0.68rem;
  border: 1px solid #dfe5df;
  border-radius: 7px;
  background: #ffffff;
  color: #35473f;
  font-size: 0.78rem;
  font-weight: 500;
  white-space: nowrap;
}

.avatar {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #08392f;
  color: #eaf3ee;
  font-size: 0.85rem;
  font-weight: 650;
}

.page-head {
  margin: 0.3rem 0 1.3rem;
}

.page-title {
  margin: 0;
  color: #10241d;
  font-size: 2.05rem;
  font-weight: 700;
  line-height: 1.08;
  letter-spacing: -0.04em;
}

.ml-eyebrow {
  color: var(--forest);
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  margin-bottom: 0.35rem;
}

.ml-sub {
  color: var(--muted);
  font-size: 0.94rem;
  margin-top: 0.2rem;
  max-width: 68ch;
}

.ml-kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.85rem;
  margin: 1.15rem 0 1rem;
}

.ml-kpi {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 0.95rem 1rem;
  box-shadow: 0 1px 2px rgba(20, 38, 33, 0.035);
}

.ml-kpi-label {
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.ml-kpi-value {
  color: var(--ink);
  font-size: 1.55rem;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
  margin-top: 0.35rem;
}

.ml-kpi-meta {
  color: var(--muted);
  font-size: 0.76rem;
  margin-top: 0.25rem;
}

.ml-next {
  align-items: center;
  background: linear-gradient(110deg, #eef6f1, #f8fbf7);
  border: 1px solid #cfe1d8;
  border-radius: 14px;
  display: flex;
  gap: 0.9rem;
  justify-content: space-between;
  margin: 0.4rem 0 1.1rem;
  padding: 0.85rem 1rem;
}

.ml-next-title {
  color: var(--ink);
  font-size: 0.92rem;
  font-weight: 650;
}

.ml-next-note {
  color: var(--muted);
  font-size: 0.8rem;
  margin-top: 0.15rem;
}

.ml-next a {
  background: var(--forest);
  border-radius: 8px;
  color: #fff !important;
  font-size: 0.78rem;
  font-weight: 600;
  padding: 0.48rem 0.7rem;
  text-decoration: none !important;
  white-space: nowrap;
}

.ml-next a:hover {
  background: var(--forest-deep);
}

.ml-flow {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.6rem;
  margin: 0 0 1rem;
}

.ml-flow-step {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  color: inherit !important;
  padding: 0.7rem 0.8rem;
  text-decoration: none !important;
  transition: border-color 150ms ease, background-color 150ms ease;
}

.ml-flow-step:hover {
  border-color: var(--line-strong);
  background: #f8fbf8;
}

.ml-flow-num {
  color: var(--forest);
  font-size: 0.66rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.ml-flow-title {
  color: var(--ink);
  font-size: 0.86rem;
  font-weight: 600;
  margin-top: 0.18rem;
}

.ml-flow-meta {
  color: var(--muted);
  font-size: 0.74rem;
  margin-top: 0.12rem;
}

.panel {
  overflow: hidden;
  border: 1px solid #e3e8e2;
  border-radius: 11px;
  background: #ffffff;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1.05rem 1.25rem 0.85rem;
}

.panel-title {
  margin: 0 !important;
  padding-top: 0;
  border-top: 0;
  color: #192b23;
  font-size: 1.02rem !important;
  font-weight: 650 !important;
  letter-spacing: -0.015em;
}

.panel-link {
  color: #0b6048;
  font-size: 0.75rem;
  font-weight: 600;
  text-decoration: none !important;
}

.panel-link:hover {
  color: #0a4c3a;
}

.table-scroll {
  overflow-x: auto;
}

.mismatch-table {
  width: 100%;
  min-width: 780px;
  border-collapse: collapse;
  font-size: 0.78rem;
}

.mismatch-table th {
  padding: 0.62rem 0.8rem;
  border-top: 1px solid #edf0ec;
  border-bottom: 1px solid #edf0ec;
  background: #fafbf9;
  color: #6f7d76;
  font-size: 0.71rem;
  font-weight: 600;
  text-align: left;
  white-space: nowrap;
}

.mismatch-table th:first-child,
.mismatch-table td:first-child {
  padding-left: 1.25rem;
}

.mismatch-table th:last-child,
.mismatch-table td:last-child {
  padding-right: 1.25rem;
}

.mismatch-table td {
  padding: 0.78rem 0.8rem;
  border-bottom: 1px solid #f0f2ef;
  color: #273b32;
  white-space: nowrap;
}

.mismatch-table tr:last-child td {
  border-bottom: 0;
}

.mismatch-table .invoice-number {
  color: #1c332a;
  font-weight: 600;
}

.mismatch-table .money {
  font-variant-numeric: tabular-nums;
}

.empty-row {
  padding: 1.5rem 1.25rem !important;
  color: #7b8982 !important;
  text-align: center !important;
}

.attention-table {
  table-layout: fixed;
  width: 100%;
  min-width: 0;
}

.attention-table th:nth-child(1),
.attention-table td:nth-child(1) { width: 22%; }

.attention-table th:nth-child(2),
.attention-table td:nth-child(2) { width: 38%; }

.attention-table th:nth-child(3),
.attention-table td:nth-child(3) { width: 24%; }

.attention-table th:nth-child(4),
.attention-table td:nth-child(4) { width: 16%; }

.attention-table td {
  overflow: hidden;
  text-overflow: ellipsis;
}

.attention-table td a {
  color: inherit !important;
  text-decoration: none !important;
}

.attention-table td a:hover {
  color: var(--forest) !important;
  text-decoration: underline !important;
}

.quick-list {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  padding: 0 0.9rem 0.95rem;
}

.quick-action {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  padding: 0.72rem 0.75rem;
  border: 1px solid #e5e9e4;
  border-radius: 8px;
  background: #fbfcfa;
  color: #253a30;
  text-decoration: none !important;
  transition: border-color 150ms ease, background-color 150ms ease;
}

.quick-action:hover {
  border-color: #a9c3b5;
  background: #f6faf7;
}

.quick-copy {
  min-width: 0;
  flex: 1;
}

.quick-title {
  display: block;
  color: #20362b;
  font-size: 0.83rem;
  font-weight: 600;
  line-height: 1.35;
}

.quick-note {
  display: block;
  margin-top: 0.1rem;
  color: #849189;
  font-size: 0.68rem;
  line-height: 1.35;
}

.quick-arrow {
  color: #8b9891;
  font-size: 0.95rem;
}

.activity-list {
  margin: 0;
  padding: 0 1.25rem 1.15rem;
  list-style: none;
}

.activity-item {
  position: relative;
  padding: 0 0 1.05rem 1.1rem;
  border-left: 1px solid #e5e9e4;
}

.activity-item:last-child {
  padding-bottom: 0;
  border-left-color: transparent;
}

.activity-dot {
  position: absolute;
  top: 0.16rem;
  left: -4px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #6b7d73;
}

.activity-dot.green {
  background: #2d7a59;
}

.activity-dot.amber {
  background: #d4a24a;
}

.activity-dot.blue {
  background: #4b8db8;
}

.activity-title {
  color: #263b31;
  font-size: 0.77rem;
  font-weight: 500;
  line-height: 1.4;
}

.activity-time {
  margin-top: 0.15rem;
  color: #8b9891;
  font-size: 0.66rem;
}

.ml-empty {
  background: var(--surface);
  border: 1px dashed var(--line-strong);
  border-radius: 16px;
  margin: 1.2rem 0;
  padding: 1.4rem 1.5rem;
}

.ml-empty h3 {
  color: var(--ink);
  font-size: 1rem;
  margin: 0 0 0.3rem;
}

.ml-empty p {
  color: var(--muted);
  font-size: 0.86rem;
  margin: 0;
  max-width: 62ch;
}

.ml-section-title {
  color: var(--ink);
  font-size: 0.98rem;
  font-weight: 650;
  margin: 1.1rem 0 0.45rem;
}

@media (max-width: 900px) {
  .ml-kpis, .ml-flow {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 860px) {
  section[data-testid="stSidebar"] {
    width: min(84vw, 300px) !important;
    min-width: 0 !important;
  }

  .block-container {
    padding: 3.6rem 1rem 2.5rem;
  }

  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .topbar-actions {
    width: 100%;
    justify-content: space-between;
  }

  .page-title {
    font-size: 1.72rem;
  }

  img[alt="Logo"] {
    display: none !important;
  }
}

@media (max-width: 640px) {
  .attention-table {
    min-width: 0 !important;
  }

  .attention-table th,
  .attention-table td {
    padding: 0.55rem 0.6rem;
  }

  .attention-table th:first-child,
  .attention-table td:first-child {
    padding-left: 0.75rem;
  }

  .attention-table th:last-child,
  .attention-table td:last-child {
    padding-right: 0.75rem;
  }

  .st-key-dash-metrics [data-testid="stHorizontalBlock"] {
    display: grid !important;
    grid-template-columns: 1fr 1fr !important;
    gap: 0.6rem !important;
  }

  .st-key-dash-metrics [data-testid="column"] {
    width: 100% !important;
    flex: 1 1 auto !important;
    min-width: 0 !important;
  }

  .st-key-dash-metrics div[data-testid="stMetric"] {
    min-height: 4.1rem;
    padding: 0.65rem 0.8rem;
  }

  .st-key-dash-metrics div[data-testid="stMetricValue"] {
    font-size: 1.35rem;
  }
}

@media (max-width: 560px) {
  .ml-kpis, .ml-flow {
    grid-template-columns: 1fr;
  }

  .ml-next {
    align-items: flex-start;
    flex-direction: column;
  }

  .topbar {
    padding: 0.85rem 1rem;
  }

  .page-title {
    font-size: 1.75rem;
  }

  .ml-sub {
    font-size: 0.88rem;
  }
}
</style>
"""


def _inject_styles() -> None:
    st.markdown(APP_CSS, unsafe_allow_html=True)


_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

_EVENT_LABELS = {
    "demo_seeded": "Demo workspace seeded",
    "invoices_validated": "Invoice validation completed",
    "reconciliation_completed": "Reconciliation completed",
    "followup_drafts_generated": "Follow-up drafts generated",
    "followup_draft_edited": "Follow-up draft edited",
    "followup_draft_approved": "Follow-up draft approved",
    "followup_draft_skipped": "Follow-up draft skipped",
    "later_statement_checked": "Later-statement check recorded",
}

_STATUS_DISPLAY = {
    reconciliation.STATUS_MISSING_FROM_2B: (
        "Not in GSTR-2B", "Missing", "red",
    ),
    reconciliation.STATUS_AMOUNT_MISMATCH: (
        "Amount mismatch", "Mismatch", "amber",
    ),
    reconciliation.STATUS_NEEDS_REVIEW: (
        "Needs review", "Review", "blue",
    ),
    reconciliation.STATUS_NOT_ELIGIBLE: (
        "Not eligible", "Excluded", "gray",
    ),
}


def _inr(amount: float) -> str:
    rounded = int(round(float(amount)))
    sign = "-" if rounded < 0 else ""
    digits = str(abs(rounded))
    if len(digits) > 3:
        last_three = digits[-3:]
        rest = digits[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        digits = ",".join(groups + [last_three])
    return f"{sign}₹{digits}"


def _parse_amount(value) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _display_date(value) -> str:
    raw = str(value or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return raw


def _display_time(value) -> str:
    raw = str(value or "")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d %b %Y · %H:%M")
        except ValueError:
            continue
    return _display_date(value)


def _period_label(period: str) -> str:
    try:
        year, month = str(period).split("-")[:2]
        return _MONTHS[int(month) - 1]
    except (ValueError, IndexError):
        return str(period)


def _activity_label(event_type: str) -> str:
    if event_type in _EVENT_LABELS:
        return _EVENT_LABELS[event_type]
    return event_type.replace("_", " ").capitalize()


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or singular + "s")
    return f"{count} {word}"


def _load_dashboard() -> dict:
    invoices = db.get_invoices()
    suppliers = db.get_suppliers()
    validation_summary = db.get_validation_summary()
    imports = db.get_gstr2b_imports(limit=20)
    latest_import = imports[0] if imports else None
    latest_run = db.get_latest_reconciliation_run()
    summary = (
        db.get_reconciliation_summary(latest_run["id"])
        if latest_run is not None else None
    )
    results = (
        db.get_reconciliation_results(latest_run["id"])
        if latest_run is not None else []
    )
    drafts = db.get_followup_drafts()
    deliveries = db.get_message_deliveries(limit=100)
    audit_events = db.get_system_audit_events(limit=6)
    supplier_gstin = {row["Supplier"]: row["GSTIN"] for row in suppliers}
    invoice_by_id = {row["ID"]: row for row in invoices}

    has_run = summary is not None
    if has_run:
        by_status = summary["by_status"]
        matched = by_status[reconciliation.STATUS_MATCHED]["count"]
        exceptions = sum(
            item["count"] for status, item in by_status.items()
            if status != reconciliation.STATUS_MATCHED
        )
        gst_exceptions = sum(
            item["total_gst_paise"] for status, item in by_status.items()
            if status != reconciliation.STATUS_MATCHED
        )
    else:
        matched = 0
        exceptions = (
            validation_summary["invalid"] + validation_summary["needs_review"]
        )
        gst_exceptions = 0

    attention = []
    validation_attention_ids = set()
    for invoice in invoices:
        if invoice["Validation"] not in ("invalid", "needs_review"):
            continue
        invalid = invoice["Validation"] == "invalid"
        validation_attention_ids.add(invoice["ID"])
        attention.append({
            "priority": 0,
            "date": str(invoice.get("Date", "")),
            "invoice": invoice["Invoice"],
            "supplier": invoice["Supplier"],
            "gstin": supplier_gstin.get(invoice["Supplier"], ""),
            "issue": "Validation failed" if invalid else "Needs review",
            "amount": _parse_amount(invoice["Total GST (Rs)"]),
            "tone": "red" if invalid else "amber",
            "href": "purchase-invoices",
        })
    if has_run:
        for result in results:
            status = result["Status"]
            if status == reconciliation.STATUS_MATCHED:
                continue
            if result["Invoice #"] in validation_attention_ids:
                continue
            invoice = invoice_by_id.get(result["Invoice #"], {})
            issue, _pill, tone = _STATUS_DISPLAY.get(
                status, (status.replace("_", " ").title(), status, "gray")
            )
            attention.append({
                "priority": 1,
                "date": str(invoice.get("Date", "")),
                "invoice": result["Invoice number"],
                "supplier": invoice.get("Supplier", "—"),
                "gstin": (
                    result["GSTIN"]
                    or supplier_gstin.get(invoice.get("Supplier"), "")
                ),
                "issue": issue,
                "amount": _parse_amount(result["Invoice GST (Rs)"]),
                "tone": tone,
                "href": "mismatches",
            })
    attention.sort(key=lambda item: (item["priority"], item["date"]),
                   reverse=False)
    attention = attention[:6]

    activity = []
    for event in audit_events:
        event_type = str(event["Event"])
        if "followup" in event_type:
            color = "amber"
        elif "later" in event_type:
            color = "blue"
        elif "reconciliation" in event_type or "delivery" in event_type:
            color = "green"
        else:
            color = "gray"
        activity.append({
            "label": _activity_label(event_type),
            "time": _display_time(event["At"]),
            "color": color,
        })

    pending_drafts = [
        row for row in drafts if row["Status"] in ("DRAFT", "NEEDS_REVIEW")
    ]
    approved_drafts = [row for row in drafts if row["Status"] == "APPROVED"]

    if not invoices:
        next_action = (
            "Add purchase invoices",
            "Import a CSV/XLSX or load the synthetic demo workspace.",
            "purchase-invoices",
            "Open purchase invoices",
        )
    elif validation_summary["invalid"] or validation_summary["needs_review"]:
        next_action = (
            "Resolve validation issues",
            f"{validation_summary['invalid']} invalid and "
            f"{validation_summary['needs_review']} needs-review invoice(s).",
            "purchase-invoices",
            "Review invoices",
        )
    elif not imports:
        next_action = (
            "Import GSTR-2B",
            "Stage a normalized GSTR-2B file, then reconcile.",
            "gstr2b-import",
            "Import GSTR-2B",
        )
    elif not has_run:
        next_action = (
            "Run reconciliation",
            "A GSTR-2B batch is ready for a deterministic match.",
            "reconciliation",
            "Run reconciliation",
        )
    elif exceptions:
        next_action = (
            "Review exceptions",
            f"{exceptions} invoice(s) need attention in the latest run.",
            "mismatches",
            "Open mismatches",
        )
    elif pending_drafts:
        next_action = (
            "Review follow-up drafts",
            f"{len(pending_drafts)} draft(s) await human review.",
            "follow-ups",
            "Review drafts",
        )
    else:
        next_action = (
            "Workspace is up to date",
            "No open validation or reconciliation exceptions.",
            "/",
            "Refresh",
        )

    return {
        "total_invoices": len(invoices),
        "supplier_count": len(suppliers),
        "validation": validation_summary,
        "imports": imports,
        "latest_import": latest_import,
        "has_run": has_run,
        "run_id": latest_run["id"] if latest_run is not None else None,
        "matched": matched,
        "exceptions": exceptions,
        "gst_exceptions": gst_exceptions,
        "attention": attention,
        "activity": activity,
        "drafts": drafts,
        "pending_drafts": pending_drafts,
        "approved_drafts": approved_drafts,
        "deliveries": deliveries,
        "next_action": next_action,
    }


def _render_ui_html(markup: str) -> None:
    compact = " ".join(
        line.strip()
        for line in textwrap.dedent(markup).splitlines()
        if line.strip()
    )
    st.markdown(compact, unsafe_allow_html=True)


def _flash(key: str) -> None:
    value = st.session_state.pop(key, None)
    if value is None:
        return
    level, message = value
    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    else:
        st.info(message)


def _render_topbar(actor: str) -> None:
    workspace = "Demo Business" if actor == "local-demo" else actor
    initial = "D" if actor == "local-demo" else (actor[:1].upper() or "M")
    _render_ui_html(
        f"""
        <div class="topbar">
          <div>
            <div class="workspace-name">
              {html.escape(workspace)}
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none"
                   aria-hidden="true">
                <path d="m4 6 4 4 4-4" stroke="currentColor"
                      stroke-width="1.5" stroke-linecap="round"
                      stroke-linejoin="round"/>
              </svg>
            </div>
            <div class="workspace-gstin">
              Synthetic workspace · Live sending off
            </div>
          </div>
          <div class="topbar-actions">
            <span class="period-chip">
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
                   aria-hidden="true">
                <rect x="2.5" y="3.5" width="11" height="10"
                      rx="1.5" stroke="currentColor" stroke-width="1.2"/>
                <path d="M5 2.5v2M11 2.5v2M2.5 6.5h11"
                      stroke="currentColor" stroke-width="1.2"
                      stroke-linecap="round"/>
              </svg>
              All periods
            </span>
            <span class="avatar">{html.escape(initial)}</span>
          </div>
        </div>
        """,
    )


def _render_sidebar(ready: bool, actor: str) -> None:
    database_path = html.escape(str(db.get_database_path().resolve()))
    status_text = "Database ready" if ready else "Database attention needed"
    with st.sidebar:
        with st.expander("Workspace status"):
            st.caption(status_text)
            st.caption(f"Database: {database_path}")
            st.caption("Live WhatsApp delivery: off")
            try:
                audit_events = db.get_system_audit_events(limit=8)
            except Exception as exc:
                st.error(f"Could not load audit history: {exc}")
            else:
                if audit_events:
                    st.dataframe(
                        audit_events,
                        alt="Recent operational audit events",
                        hide_index=True,
                        height=200,
                    )
                else:
                    st.caption("No operational actions recorded yet.")
        if actor != "local-demo":
            st.caption(f"Signed in as {actor}")
            if st.button("Sign out", key="sign_out"):
                st.logout()
        _render_ui_html(
            """
            <div class="sb-footer">
              Match. Detect. Resolve.
              <div class="sb-version">v1.0.0 · Live sending off</div>
            </div>
            """,
        )


def _require_authorized_user() -> str:
    try:
        config = security.load_security_config()
    except ValueError as exc:
        st.error(f"Security configuration error: {exc}")
        st.stop()
    if not config.auth_required:
        return "local-demo"
    try:
        is_logged_in = bool(st.user.is_logged_in)
    except AttributeError:
        st.title("MILAAN sign-in required")
        st.error(
            "OIDC is required but has not been configured. Add valid provider "
            "settings to .streamlit/secrets.toml before serving this workspace."
        )
        st.stop()
    if not is_logged_in:
        st.title("MILAAN sign-in required")
        st.info(
            "This production workspace requires an approved OIDC identity. "
            "Configure .streamlit/secrets.toml from secrets.example.toml "
            "before signing in."
        )
        if st.button("Sign in", key="sign_in"):
            st.login()
        st.stop()
    email = getattr(st.user, "email", None)
    if not security.email_is_allowed(email, config.allowed_email_domains):
        st.error("Your signed-in email is not authorized for this workspace.")
        if st.button("Sign out", key="unauthorized_sign_out"):
            st.logout()
        st.stop()
    return str(email).strip().lower()


def _audit(actor: str, event: str, subject: str, subject_id: object,
           **details: object) -> None:
    try:
        db.record_system_audit_event(
            actor=actor,
            event_type=event,
            subject_type=subject,
            subject_id=str(subject_id),
            details=details,
        )
    except Exception as exc:
        st.warning(
            f"Action completed, but its operational audit entry failed: {exc}"
        )


def _page_shell(title: str, subtitle: str, eyebrow: str = "Workspace") -> str:
    _inject_styles()
    actor = _require_authorized_user()
    try:
        db.init_db()
        ready = db.is_db_ready()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        st.stop()
    _render_topbar(actor)
    _render_sidebar(ready, actor)
    _render_ui_html(
        f"""
        <div class="page-head">
          <div class="ml-eyebrow">{html.escape(eyebrow)}</div>
          <h1 class="page-title">{html.escape(title)}</h1>
          <div class="ml-sub">{html.escape(subtitle)}</div>
        </div>
        """,
    )
    return actor


def _page_link_href(path: str) -> str:
    return path


def _render_dashboard(data: dict, actor: str = "local-demo") -> None:
    total = data["total_invoices"]
    validation_summary = data["validation"]
    latest_import = data["latest_import"]
    pending = data["pending_drafts"]
    next_title, next_note, next_href, next_label = data["next_action"]

    with st.container(key="dash-metrics"):
        metric_cols = st.columns(4)
        metric_cols[0].metric("Invoices", total, border=True)
        metric_cols[1].metric(
            "Valid", validation_summary["valid"], border=True
        )
        metric_cols[2].metric(
            "Invalid", validation_summary["invalid"], border=True
        )
        metric_cols[3].metric(
            "Needs review", validation_summary["needs_review"], border=True
        )

    batch_meta = "No batch imported"
    if latest_import is not None:
        batch_meta = (
            f"Batch #{latest_import['Batch']} · "
            f"{latest_import['Accepted']}/{latest_import['Rows']} accepted"
        )
    run_meta = "No reconciliation run"
    if data["has_run"]:
        run_meta = f"Run #{data['run_id']} · {data['matched']} matched"
    draft_meta = (
        f"{len(pending)} awaiting review"
        if pending else "No drafts awaiting review"
    )
    delivery_meta = (
        f"{len(data['deliveries'])} recorded · mock only"
        if data["deliveries"] else "No delivery attempts"
    )

    _render_ui_html(
        f"""
        <div class="ml-kpis">
          <div class="ml-kpi">
            <div class="ml-kpi-label">GSTR-2B batches</div>
            <div class="ml-kpi-value">{_plural(len(data['imports']), 'batch', 'batches')}</div>
            <div class="ml-kpi-meta">{html.escape(batch_meta)}</div>
          </div>
          <div class="ml-kpi">
            <div class="ml-kpi-label">Reconciliation exceptions</div>
            <div class="ml-kpi-value">{data['exceptions']:,}</div>
            <div class="ml-kpi-meta">{html.escape(run_meta)}</div>
          </div>
          <div class="ml-kpi">
            <div class="ml-kpi-label">Follow-up drafts</div>
            <div class="ml-kpi-value">{len(data['drafts'])}</div>
            <div class="ml-kpi-meta">{html.escape(draft_meta)}</div>
          </div>
          <div class="ml-kpi">
            <div class="ml-kpi-label">Delivery evidence</div>
            <div class="ml-kpi-value">{len(data['deliveries'])}</div>
            <div class="ml-kpi-meta">{html.escape(delivery_meta)}</div>
          </div>
        </div>

        <div class="ml-next">
          <div>
            <div class="ml-next-title">{html.escape(next_title)}</div>
            <div class="ml-next-note">{html.escape(next_note)}</div>
          </div>
          <a href="{html.escape(next_href)}">{html.escape(next_label)}</a>
        </div>

        <div class="ml-flow">
          <a class="ml-flow-step" href="purchase-invoices">
            <div class="ml-flow-num">01 · Purchase data</div>
            <div class="ml-flow-title">{total} invoice(s)</div>
            <div class="ml-flow-meta">
              {validation_summary['valid']} valid · {validation_summary['invalid']} invalid
            </div>
          </a>
          <a class="ml-flow-step" href="gstr2b-import">
            <div class="ml-flow-num">02 · GSTR-2B</div>
            <div class="ml-flow-title">
              {_plural(len(data['imports']), 'batch', 'batches')}
            </div>
            <div class="ml-flow-meta">
              {html.escape(batch_meta)}
            </div>
          </a>
          <a class="ml-flow-step" href="reconciliation">
            <div class="ml-flow-num">03 · Reconciliation</div>
            <div class="ml-flow-title">
              {data['matched']} matched
            </div>
            <div class="ml-flow-meta">
              {data['exceptions']} exception(s)
            </div>
          </a>
          <a class="ml-flow-step" href="follow-ups">
            <div class="ml-flow-num">04 · Follow-ups</div>
            <div class="ml-flow-title">{len(data['drafts'])} draft(s)</div>
            <div class="ml-flow-meta">
              {len(pending)} awaiting review
            </div>
          </a>
        </div>
        """,
    )

    attention_rows = "".join(
        f"""
        <tr>
          <td class="invoice-number">
            <a href="{html.escape(item["href"])}">
              {html.escape(str(item["invoice"]))}
            </a>
          </td>
          <td title="{html.escape(str(item["supplier"]))}">
            {html.escape(str(item["supplier"]))}
          </td>
          <td>{html.escape(item["issue"])}</td>
          <td class="money">
            {html.escape(_inr(item["amount"]).replace("₹", ""))}
          </td>
        </tr>
        """
        for item in data["attention"]
    )
    if not attention_rows:
        attention_rows = (
            '<tr><td class="empty-row" colspan="4">'
            "No validation or reconciliation exceptions right now."
            "</td></tr>"
        )

    activity_html = "".join(
        f"""
        <li class="activity-item">
          <span class="activity-dot {item["color"]}"></span>
          <div class="activity-title">{html.escape(item["label"])}</div>
          <div class="activity-time">{html.escape(item["time"])}</div>
        </li>
        """
        for item in data["activity"]
    )
    if not activity_html:
        activity_html = (
            '<li class="activity-item"><span class="activity-dot"></span>'
            '<div class="activity-title">No activity recorded yet.</div>'
            '<div class="activity-time">Import data or load the demo '
            "workspace to begin.</div></li>"
        )

    left, right = st.columns([1.55, 1], gap="large")
    with left:
        _render_ui_html(
            f"""
            <section class="panel">
              <div class="panel-head">
                <h2 class="panel-title">Needs attention</h2>
                <a class="panel-link" href="mismatches">Open exceptions →</a>
              </div>
              <div class="table-scroll">
                <table class="mismatch-table attention-table">
                  <thead>
                    <tr>
                      <th>Invoice</th>
                      <th>Supplier</th>
                      <th>Issue</th>
                      <th>GST (₹)</th>
                    </tr>
                  </thead>
                  <tbody>{attention_rows}</tbody>
                </table>
              </div>
            </section>
            """,
        )
        if st.button("Validate all invoices", key="validate_all",
                     type="primary"):
            try:
                results = db.revalidate_all_invoices()
                _audit(actor=actor, event="invoices_validated",
                       subject="invoice_set", subject_id="all",
                       count=len(results))
            except Exception as exc:
                st.error(f"Validation failed: {exc}")
            else:
                counts = {status: 0 for status in validation.STATUSES}
                for entry in results:
                    counts[entry["result"].status] += 1
                if not results:
                    st.session_state["dashboard_flash"] = (
                        "info", "No invoices to validate yet."
                    )
                else:
                    st.session_state["dashboard_flash"] = (
                        "success",
                        f"Validated {len(results)} invoice(s): "
                        f"{counts['valid']} valid, "
                        f"{counts['needs_review']} need review, "
                        f"{counts['invalid']} invalid.",
                    )
                st.rerun()

    with right:
        _render_ui_html(
            f"""
            <section class="panel">
              <div class="panel-head">
                <h2 class="panel-title">Recent activity</h2>
                <a class="panel-link" href="audit-log">View audit →</a>
              </div>
              <ul class="activity-list">{activity_html}</ul>
            </section>
            <section class="panel" style="margin-top:0.85rem">
              <div class="panel-head">
                <h2 class="panel-title">Quick actions</h2>
              </div>
              <div class="quick-list">
                <a class="quick-action" href="purchase-invoices">
                  <span class="quick-copy">
                    <span class="quick-title">Import purchase invoices</span>
                    <span class="quick-note">CSV or XLSX · validated on import</span>
                  </span>
                  <span class="quick-arrow">›</span>
                </a>
                <a class="quick-action" href="gstr2b-import">
                  <span class="quick-copy">
                    <span class="quick-title">Stage GSTR-2B</span>
                    <span class="quick-note">Preview before importing</span>
                  </span>
                  <span class="quick-arrow">›</span>
                </a>
                <a class="quick-action" href="reconciliation">
                  <span class="quick-copy">
                    <span class="quick-title">Run reconciliation</span>
                    <span class="quick-note">Exact match · zero tolerance by default</span>
                  </span>
                  <span class="quick-arrow">›</span>
                </a>
                <a class="quick-action" href="follow-ups">
                  <span class="quick-copy">
                    <span class="quick-title">Review follow-ups</span>
                    <span class="quick-note">Drafts · approval · mock delivery</span>
                  </span>
                  <span class="quick-arrow">›</span>
                </a>
              </div>
            </section>
            """,
        )

    with st.expander("Demo workspace", expanded=False):
        st.caption(
            "Load a complete synthetic pipeline: 6 purchase invoices, "
            "one GSTR-2B batch with an exact match, an amount mismatch and "
            "a rejected row, plus a reconciliation run. Live sending stays off."
        )
        if st.button("Load demo workspace", key="seed_demo"):
            _load_demo_workspace()


def _load_demo_workspace() -> None:
    try:
        result = demo_workflow.load_demo_workspace()
    except Exception as exc:
        st.error(f"Demo workspace failed: {exc}")
        return
    _audit(
        "local-demo", "demo_seeded", "database", db.get_database_path(),
        **result,
    )
    st.session_state["dashboard_flash"] = (
        "success",
        f"Demo workspace ready: {result['seeded']['suppliers']} supplier(s) "
        f"added, {result['seeded']['invoices']} invoice(s) added. "
        f"Validation: {result['validation']['valid']} valid, "
        f"{result['validation']['invalid']} invalid. "
        "No message was sent.",
    )
    st.rerun()


def page_dashboard() -> None:
    actor = _page_shell(
        "Dashboard",
        "A compact view of purchase data, GSTR-2B evidence, and what needs "
        "attention next.",
        "Overview",
    )
    _flash("dashboard_flash")
    try:
        data = _load_dashboard()
    except Exception as exc:
        st.error(f"Could not load dashboard: {exc}")
        return
    if data["total_invoices"] == 0 and not data["imports"]:
        _render_ui_html(
            """
            <div class="ml-empty">
              <h3>Start with purchase invoices</h3>
              <p>
                Import a CSV or XLSX file, or load the synthetic demo
                workspace. MILAAN validates every row before anything is
                reconciled, and never sends a message on its own.
              </p>
            </div>
            """,
        )
        col_a, col_b = st.columns([1, 1])
        with col_a:
            if st.button("Load demo workspace", key="seed_demo",
                         type="primary"):
                _load_demo_workspace()
        with col_b:
            st.page_link(
                "pages/2_purchase_invoices.py",
                label="Import purchase invoices",
                icon=":material/upload_file:",
            )
        return
    _render_dashboard(data, actor)


def page_purchase_invoices() -> None:
    actor = _page_shell(
        "Purchase invoices",
        "Import, validate, and inspect supplier invoices. Validation is a "
        "field check — it does not verify live GST registration or ITC "
        "eligibility.",
        "Workflow · 01",
    )
    _flash("purchase_flash")
    try:
        totals = db.get_totals()
        summary = db.get_validation_summary()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return

    cols = st.columns(4)
    cols[0].metric("Invoices", totals["invoice_count"], border=True)
    cols[1].metric("Valid", summary["valid"], border=True)
    cols[2].metric("Invalid", summary["invalid"], border=True)
    cols[3].metric("Needs review", summary["needs_review"], border=True)

    with st.container(border=True):
        st.subheader("Import purchase invoices")
        st.caption(
            "CSV or XLSX. Amounts are rupee decimals; MILAAN stores integer "
            "paise. Duplicate natural keys are reported, never overwritten."
        )
        uploaded = st.file_uploader(
            "Purchase invoice file",
            type=["csv", "xlsx"],
            key="purchase_upload",
        )
        if uploaded is not None:
            try:
                parsed = invoice_import.parse_invoice_file(
                    uploaded.getvalue(), uploaded.name
                )
            except ValueError as exc:
                st.error(f"Could not parse file: {exc}")
            else:
                st.caption(
                    f"{parsed.total_rows} row(s): "
                    f"{parsed.accepted_rows} ready, "
                    f"{parsed.rejected_rows} rejected. "
                    f"SHA-256 {parsed.file_hash[:12]}…"
                )
                preview = []
                for row in parsed.rows:
                    issues = "; ".join(
                        f"{issue.code}: {issue.message}"
                        for issue in (*row.errors, *row.warnings)
                    )
                    preview.append({
                        "Row": row.row_number,
                        "Supplier": row.supplier_name,
                        "GSTIN": row.supplier_gstin,
                        "Invoice": row.invoice_number,
                        "Date": row.invoice_date,
                        "Period": row.tax_period,
                        "Taxable (₹)": db.format_paise(row.taxable_value),
                        "GST (₹)": db.format_paise(row.total_gst),
                        "Status": row.validation_status,
                        "Issues": issues,
                    })
                st.dataframe(
                    preview,
                    alt="Purchase invoice import preview with row validation",
                    hide_index=True,
                )
                if st.button(
                    "Import valid rows",
                    key="purchase_import",
                    type="primary",
                    disabled=parsed.accepted_rows == 0,
                ):
                    try:
                        result = db.save_invoice_import(parsed)
                        _audit(
                            actor, "purchase_invoices_imported", "invoice_set",
                            parsed.file_name,
                            imported=result["imported"],
                            duplicates=result["duplicates"],
                            rejected=result["rejected"],
                        )
                    except Exception as exc:
                        st.error(f"Import failed: {exc}")
                    else:
                        st.session_state["purchase_flash"] = (
                            "success",
                            f"Imported {result['imported']} invoice(s); "
                            f"{result['duplicates']} duplicate(s), "
                            f"{result['rejected']} rejected row(s). "
                            "No message was sent.",
                        )
                        st.rerun()
        st.download_button(
            "Download CSV template",
            data=invoice_import.template_csv(),
            file_name="MILAAN_purchase_invoices_template.csv",
            mime="text/csv",
            key="purchase_template",
        )

    toolbar_left, toolbar_right = st.columns([1, 1])
    with toolbar_left:
        if st.button("Validate all invoices", key="purchase_validate_all",
                     type="primary"):
            try:
                results = db.revalidate_all_invoices()
                _audit(
                    actor, "invoices_validated", "invoice_set", "all",
                    count=len(results),
                )
            except Exception as exc:
                st.error(f"Validation failed: {exc}")
            else:
                counts = {status: 0 for status in validation.STATUSES}
                for entry in results:
                    counts[entry["result"].status] += 1
                st.session_state["purchase_flash"] = (
                    "success",
                    f"Validated {len(results)} invoice(s): "
                    f"{counts['valid']} valid, "
                    f"{counts['needs_review']} need review, "
                    f"{counts['invalid']} invalid.",
                )
                st.rerun()
    with toolbar_right:
        st.caption(
            "A valid result means GSTIN structure, arithmetic, dates and "
            "duplicate checks passed. It is not an ITC decision."
        )

    try:
        invoices = db.get_invoices()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return
    if not invoices:
        st.info(
            "No purchase invoices yet. Import a file above or load the demo "
            "workspace from the Dashboard."
        )
        return

    filter_value = st.selectbox(
        "Show invoices",
        ["All", "Valid", "Needs review", "Invalid", "Pending"],
        key="purchase_filter",
    )
    if filter_value == "All":
        rows = invoices
    elif filter_value == "Pending":
        rows = [
            row for row in invoices
            if row["Validation"] in ("pending", "mismatch")
        ]
    else:
        rows = [
            row for row in invoices
            if row["Validation"] == filter_value.lower()
        ]
    st.caption(f"{len(rows)} of {len(invoices)} invoice(s)")
    st.dataframe(
        [
            {
                "Invoice": row["Invoice"],
                "Supplier": row["Supplier"],
                "Date": row["Date"],
                "Period": row["Tax period"],
                "Taxable (₹)": row["Taxable (Rs)"],
                "Total GST (₹)": row["Total GST (Rs)"],
                "Validation": row["Validation"],
            }
            for row in rows
        ],
        alt="Purchase invoices with validation status",
        hide_index=True,
    )

    issues = st.session_state.get("purchase_validation_results")
    if issues:
        with st.expander("Last validation details", expanded=False):
            for item in issues:
                st.markdown(
                    f"**{item['invoice_number']}** — `{item['status']}`"
                )
                for issue in item["issues"]:
                    st.write(f"- {issue}")


def page_gstr2b_import() -> None:
    actor = _page_shell(
        "GSTR-2B import",
        "Stage a normalized GSTR-2B file with a complete audit trail. "
        "Importing never changes purchase invoices or runs reconciliation.",
        "Workflow · 02",
    )
    _flash("gstr2b_flash")
    try:
        imports = db.get_gstr2b_imports()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return

    cols = st.columns(3)
    cols[0].metric("Batches", len(imports), border=True)
    cols[1].metric(
        "Accepted rows",
        sum(row["Accepted"] for row in imports),
        border=True,
    )
    cols[2].metric(
        "Rejected rows",
        sum(row["Rejected"] for row in imports),
        border=True,
    )

    with st.container(border=True):
        st.subheader("Stage a GSTR-2B file")
        st.caption(
            "Required columns: supplier_gstin, invoice_number, invoice_date "
            "(YYYY-MM-DD), tax_period (YYYY-MM), taxable_value, cgst, sgst, "
            "igst, total_gst. XLSX reads the active worksheet."
        )
        uploaded = st.file_uploader(
            "GSTR-2B file", type=["csv", "xlsx"], key="gstr2b_upload"
        )
        if uploaded is not None:
            content = uploaded.getvalue()
            file_hash = gstr2b.sha256_hex(content)
            prior_imports = db.find_gstr2b_imports_by_hash(file_hash)
            parsed = None
            parse_error = None
            try:
                parsed = gstr2b.parse_gstr2b(content, uploaded.name)
            except ValueError as exc:
                parse_error = exc
            if prior_imports:
                st.warning(
                    f"This exact file was already imported as batch "
                    f"{', '.join('#' + str(row['id']) for row in prior_imports)}. "
                    "Importing again creates a separate batch; it is never "
                    "silently deduplicated."
                )
                confirm_reimport = st.checkbox(
                    "Import this identical file again as a new batch",
                    key="gstr2b_reimport_confirm",
                )
            else:
                confirm_reimport = True
                st.caption(f"SHA-256: {file_hash}")
            if parse_error is not None:
                st.error(f"Could not parse file: {parse_error}")
            elif parsed is not None:
                st.caption(
                    f"{parsed.total_rows} row(s): "
                    f"{parsed.accepted_rows} accepted, "
                    f"{parsed.rejected_rows} rejected."
                )
                preview = []
                for row in parsed.rows:
                    issues = "; ".join(
                        f"{issue.code}: {issue.message}"
                        for issue in (*row.errors, *row.warnings)
                    )
                    preview.append({
                        "Row": row.row_number,
                        "GSTIN": row.supplier_gstin,
                        "Invoice": row.invoice_number,
                        "Date": row.invoice_date,
                        "Period": row.tax_period,
                        "Taxable (₹)": db.format_paise(row.taxable_value),
                        "GST (₹)": db.format_paise(row.total_gst),
                        "Status": row.validation_status,
                        "Issues": issues,
                    })
                st.dataframe(
                    preview,
                    alt="GSTR-2B preview with row-level validation",
                    hide_index=True,
                )
                if st.button(
                    "Import GSTR-2B file",
                    key="gstr2b_import",
                    type="primary",
                    disabled=not confirm_reimport,
                ):
                    try:
                        saved = db.save_gstr2b_import(parsed)
                        _audit(
                            actor, "gstr2b_imported", "gstr2b_import",
                            saved["import_id"],
                            file_name=saved["file_name"],
                            accepted_rows=saved["accepted_rows"],
                            rejected_rows=saved["rejected_rows"],
                        )
                    except Exception as exc:
                        st.error(f"GSTR-2B import failed: {exc}")
                    else:
                        st.session_state["gstr2b_flash"] = (
                            "success",
                            f"Batch #{saved['import_id']} imported: "
                            f"{saved['accepted_rows']} of "
                            f"{saved['total_rows']} row(s) accepted, "
                            f"{saved['rejected_rows']} rejected. "
                            "All source rows are kept in the audit trail.",
                        )
                        st.rerun()

    if not imports:
        st.info("No GSTR-2B imports yet.")
        return

    st.markdown('<div class="ml-section-title">Import batches</div>',
                unsafe_allow_html=True)
    st.dataframe(
        imports,
        alt="Imported normalized GSTR-2B batches",
        hide_index=True,
    )
    batch_options = {
        f"Batch #{row['Batch']} — {row['File']} "
        f"({row['Accepted']}/{row['Rows']} accepted)": row["Batch"]
        for row in imports
    }
    selected_batch_label = st.selectbox(
        "Inspect batch",
        options=list(batch_options),
        key="gstr2b_batch",
    )
    batch_rows = db.get_gstr2b_rows(batch_options[selected_batch_label])
    if not batch_rows:
        st.info("This batch has no rows.")
        return
    st.dataframe(
        batch_rows,
        alt="Rows and validation outcomes in the selected import batch",
        hide_index=True,
    )
    rejected_rows = [row for row in batch_rows if row["Status"] != "valid"]
    if rejected_rows:
        with st.expander("Row-level issues", expanded=True):
            for row in rejected_rows:
                st.markdown(
                    f"**Row {row['Row']}** — status `{row['Status']}`"
                )
                st.write(row["Issues"])


def page_reconciliation() -> None:
    actor = _page_shell(
        "Reconciliation",
        "Match validated invoices to one GSTR-2B batch on GSTIN, invoice "
        "number, period, and exact GST amounts. Ambiguity goes to review.",
        "Workflow · 03",
    )
    _flash("recon_flash")
    try:
        imports = db.get_gstr2b_imports()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return
    if not imports:
        _render_ui_html(
            """
            <div class="ml-empty">
              <h3>Stage a GSTR-2B batch first</h3>
              <p>
                Reconciliation needs a selected batch and at least one
                validated purchase invoice. Import a batch, then return here.
              </p>
            </div>
            """,
        )
        st.page_link(
            "pages/3_gstr2b_import.py",
            label="Open GSTR-2B import",
            icon=":material/upload_file:",
        )
        return

    batch_labels = {
        f"Batch #{row['Batch']} — {row['File']} "
        f"({row['Accepted']}/{row['Rows']} accepted)": row["Batch"]
        for row in imports
    }
    batch_column, tolerance_column = st.columns([2.4, 1])
    selected_label = batch_column.selectbox(
        "GSTR-2B batch to reconcile",
        options=list(batch_labels),
        key="recon_batch",
    )
    tolerance = tolerance_column.number_input(
        "Amount tolerance (paise; 0 = exact)",
        min_value=0,
        value=0,
        step=1,
        key="recon_tolerance",
    )
    st.caption(
        "Exact-match-first on normalized GSTIN + invoice number + tax period. "
        "GST amounts are compared in integer paise. A missing row is not "
        "proof of supplier non-filing or ITC eligibility."
    )
    if st.button("Reconcile selected batch", key="reconcile",
                 type="primary"):
        try:
            summary = db.reconcile_gstr2b_import(
                batch_labels[selected_label], int(tolerance)
            )
            _audit(
                actor, "reconciliation_completed", "reconciliation_run",
                summary["run_id"],
                batch_id=summary["gstr2b_import_id"],
                tolerance_paise=summary["amount_tolerance_paise"],
            )
        except ValueError as exc:
            st.warning(str(exc))
        except Exception as exc:
            st.error(f"Reconciliation failed: {exc}")
        else:
            message = (
                f"Run #{summary['run_id']} completed for batch "
                f"#{summary['gstr2b_import_id']}: "
                f"{summary['matched_count']} matched, "
                f"{summary['amount_mismatch_count']} amount mismatch, "
                f"{summary['missing_count']} missing from 2B, "
                f"{summary['needs_review_count']} needs review, "
                f"{summary['not_eligible_count']} not eligible."
            )
            st.session_state["recon_flash"] = (
                "success" if summary["matched_count"] else "warning",
                message,
            )
            st.rerun()

    try:
        runs = db.get_reconciliation_runs()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return
    if not runs:
        st.info("No reconciliation runs yet.")
        return

    st.markdown('<div class="ml-section-title">Run history</div>',
                unsafe_allow_html=True)
    st.dataframe(
        [
            {
                "Run": row["Run"],
                "Created": row["Created at"],
                "Batch": row["Batch"],
                "Invoices": row["Invoices"],
                "Matched": row["Matched"],
                "Amount mismatch": row["Amount mismatch"],
                "Missing from 2B": row["Missing from 2B"],
                "Needs review": row["Needs review"],
                "Not eligible": row["Not eligible"],
            }
            for row in runs
        ],
        alt="Persisted reconciliation runs",
        hide_index=True,
    )
    selected_run = st.selectbox(
        "Inspect reconciliation run",
        options=[row["Run"] for row in runs],
        key="recon_run",
    )
    run_summary = db.get_reconciliation_summary(selected_run)
    if run_summary is None:
        return
    by_status = run_summary["by_status"]
    status_specs = (
        ("MATCHED", "Matched"),
        ("AMOUNT_MISMATCH", "Amount mismatch"),
        ("MISSING_FROM_2B", "Missing from 2B"),
        ("NEEDS_REVIEW", "Needs review"),
        ("NOT_ELIGIBLE", "Not eligible"),
    )
    for row_specs in (status_specs[:3], status_specs[3:]):
        columns = st.columns(len(row_specs))
        for column, (status, label) in zip(columns, row_specs):
            item = by_status[status]
            column.metric(
                label,
                f"{item['count']} · Rs "
                f"{db.format_paise(item['total_gst_paise'])}",
                border=True,
            )

    run = run_summary["run"]
    st.caption(
        f"Selected run #{run['id']} (batch #{run['gstr2b_import_id']}, "
        f"{run['file_name']}), tolerance "
        f"{run['amount_tolerance_paise']} paise, created "
        f"{run['created_at']}. Rupee totals are invoice GST per status. "
        "A missing row is not evidence of recoverable ITC."
    )
    if by_status["MATCHED"]["count"] == 0:
        st.warning(
            "This run has zero matched invoices — inspect the evidence "
            "table and reason codes below."
        )
    st.dataframe(
        [
            {
                "Invoice": row["Invoice number"],
                "GSTIN": row["GSTIN"],
                "Period": row["Tax period"],
                "Invoice GST (₹)": row["Invoice GST (Rs)"],
                "Status": row["Status"],
                "2B GST (₹)": row["2B GST (Rs)"],
                "Difference (₹)": row["Difference (Rs)"],
                "Reasons": row["Reasons"],
            }
            for row in db.get_reconciliation_results(selected_run)
        ],
        alt="Invoice-level reconciliation statuses and evidence",
        hide_index=True,
    )


def page_mismatches() -> None:
    _page_shell(
        "Mismatches",
        "Inspect invoice-level evidence from a reconciliation run. Every "
        "status is deterministic and traceable to the selected batch.",
        "Workflow · 04",
    )
    try:
        runs = db.get_reconciliation_runs()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return
    if not runs:
        _render_ui_html(
            """
            <div class="ml-empty">
              <h3>No reconciliation evidence yet</h3>
              <p>
                Import a GSTR-2B batch and run reconciliation. This page
                shows only persisted evidence — never inferred matches.
              </p>
            </div>
            """,
        )
        st.page_link(
            "pages/4_reconciliation.py",
            label="Open reconciliation",
            icon=":material/compare_arrows:",
        )
        return

    selected_run = st.selectbox(
        "Reconciliation run",
        options=[row["Run"] for row in runs],
        key="mismatch_run",
    )
    results = db.get_reconciliation_results(selected_run)
    statuses = sorted({row["Status"] for row in results})
    status_filter = st.selectbox(
        "Status",
        ["All exceptions"] + statuses,
        key="mismatch_status",
    )
    if status_filter == "All exceptions":
        rows = [
            row for row in results
            if row["Status"] != reconciliation.STATUS_MATCHED
        ]
    elif status_filter == "All":
        rows = results
    else:
        rows = [row for row in results if row["Status"] == status_filter]

    st.caption(
        f"{len(rows)} row(s) in run #{selected_run}. "
        "Status labels describe this batch only."
    )
    if not rows:
        st.success("No rows match this filter.")
        return
    st.dataframe(
        rows,
        alt="Invoice-level reconciliation evidence and reason codes",
        hide_index=True,
    )

    options = {
        f"{row['Invoice number']} — {row['Status']}": row for row in rows
    }
    selected = st.selectbox(
        "Inspect evidence", options=list(options), key="mismatch_detail"
    )
    detail = options[selected]
    st.markdown(
        f"**{detail['Invoice number']}** · `{detail['Status']}` · "
        f"method `{detail['Method'] or 'none'}`"
    )
    st.json(detail, expanded=False)
    if detail["2B row"]:
        try:
            row = db.get_gstr2b_row(int(detail["2B row"]))
        except (TypeError, ValueError):
            row = None
        if row is not None:
            with st.expander("GSTR-2B source row"):
                st.json(row, expanded=False)


def page_followups() -> None:
    actor = _page_shell(
        "Supplier follow-ups",
        "Draft follow-ups only for validated invoices with an unambiguous "
        "missing-from-2B result. Human approval records review; live delivery "
        "remains disabled.",
        "Workflow · 05",
    )
    _flash("followup_flash")
    try:
        imports = db.get_gstr2b_imports()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return
    if not imports:
        _render_ui_html(
            """
            <div class="ml-empty">
              <h3>Follow-ups need reconciliation evidence</h3>
              <p>
                Import a GSTR-2B batch and run reconciliation first. Draft
                eligibility is based only on the latest persisted run.
              </p>
            </div>
            """,
        )
        st.page_link(
            "pages/4_reconciliation.py",
            label="Open reconciliation",
            icon=":material/compare_arrows:",
        )
        return

    draft_tab, later_tab, delivery_tab = st.tabs(
        ["Draft review", "Later statement", "Delivery"]
    )
    with draft_tab:
        _render_followup_drafts(actor, imports)
    with later_tab:
        _render_later_statement(actor)
    with delivery_tab:
        _render_delivery(actor)


def _render_followup_drafts(actor: str, imports: list[dict]) -> None:
    draft_batch_labels = {
        f"Batch #{row['Batch']} — {row['File']} "
        f"({row['Accepted']}/{row['Rows']} accepted)": row["Batch"]
        for row in imports
    }
    draft_batch_label = st.selectbox(
        "GSTR-2B batch for follow-up preparation",
        options=list(draft_batch_labels),
        key="followup_batch",
    )
    draft_batch_id = draft_batch_labels[draft_batch_label]
    try:
        eligibility = db.get_followup_eligibility(draft_batch_id)
    except Exception as exc:
        st.error(f"Could not evaluate follow-up eligibility: {exc}")
        return
    followup_run = eligibility["run"]
    if followup_run is None:
        st.info(
            f"No reconciliation run exists for batch #{draft_batch_id} yet. "
            "Reconcile this batch first."
        )
        return
    st.caption(
        f"Using the latest reconciliation run #{followup_run['id']} for "
        f"batch #{draft_batch_id} ({followup_run['file_name']})."
    )
    plans = eligibility["plans"]
    excluded = eligibility["excluded"]
    if plans:
        summary_rows = []
        for plan in plans:
            summary_rows.append({
                "Supplier": plan["supplier_name"],
                "GSTIN": plan["supplier_gstin"],
                "Invoices": len(plan["invoices"]),
                "Taxable (Rs)": db.format_paise(
                    plan["total_taxable_value_paise"]
                ),
                "GST (Rs)": db.format_paise(plan["total_gst_paise"]),
                "Phone on file": plan["contact_phone"] or "not on file",
                "Channel": (
                    "available" if plan["channel_available"]
                    else "unavailable"
                ),
                "Planned status": plan["status"],
                "Review reasons": ", ".join(plan["review_reasons"]),
            })
        st.markdown("**Eligible supplier groups (preview)**")
        st.dataframe(
            summary_rows,
            alt="Supplier groups eligible for follow-up review",
            hide_index=True,
        )
        with st.expander("Preview planned draft text"):
            for plan in plans:
                st.markdown(
                    f"**{plan['supplier_name']}** "
                    f"(`{plan['supplier_gstin'] or 'no GSTIN'}`)"
                )
                st.text(plan["draft_text"])
        if st.button("Generate follow-up drafts", key="generate_followups",
                     type="primary"):
            try:
                generated = db.generate_followup_drafts(draft_batch_id)
                _audit(
                    actor, "followup_drafts_generated", "gstr2b_import",
                    draft_batch_id,
                    created=generated["created_drafts"],
                    existing=generated["existing_drafts"],
                )
            except ValueError as exc:
                st.warning(str(exc))
            except Exception as exc:
                st.error(f"Draft generation failed: {exc}")
            else:
                st.session_state["followup_flash"] = (
                    "success",
                    f"{generated['created_drafts']} draft(s) created, "
                    f"{generated['existing_drafts']} already existed for this "
                    "run (existing drafts and edits were left untouched). "
                    "No message was sent.",
                )
                st.rerun()
    else:
        st.warning(
            "No invoices in this run are eligible for supplier follow-up "
            "drafts. Nothing was generated."
        )

    if excluded:
        with st.expander("Ineligible invoices and why"):
            st.dataframe([
                {
                    "Invoice #": item["invoice_id"],
                    "Invoice number": item["invoice_number"],
                    "Supplier": item["supplier_name"],
                    "Invoice validation": item["validation_status"],
                    "Reconciliation": item["match_status"],
                    "Excluded because": item["reason"],
                }
                for item in excluded
            ], alt="Invoices excluded from follow-up and reasons",
                hide_index=True)
    else:
        st.caption("No invoices were excluded in this run.")

    drafts = db.get_followup_drafts(gstr2b_import_id=draft_batch_id)
    if not drafts:
        st.info(
            "No follow-up drafts saved yet for this batch. Preview the "
            "eligible groups above, then generate drafts."
        )
        return
    st.markdown("**Saved drafts for this batch**")
    st.dataframe(
        drafts,
        alt="Saved follow-up drafts and status",
        hide_index=True,
    )
    draft_labels = {
        f"#{row['Draft']} — {row['Supplier']} "
        f"(run #{row['Run']}, {row['Status']})": row["Draft"]
        for row in drafts
    }
    selected_draft_label = st.selectbox(
        "Inspect draft",
        options=list(draft_labels),
        key="followup_draft",
    )
    draft_id = draft_labels[selected_draft_label]
    draft = db.get_followup_draft(draft_id)
    if draft is None:
        return
    editable = draft["status"] in ("DRAFT", "NEEDS_REVIEW")
    st.markdown(
        f"**Draft #{draft['id']} — {draft['supplier_name']}**  \n"
        f"Status: `{draft['status']}` · Language: `{draft['language']}` · "
        f"Phone on file: {draft['contact_phone'] or 'none'} · "
        f"Delivery channel: "
        f"{'available' if draft['channel_available'] else 'unavailable'}"
    )
    st.warning(
        "No message has been sent yet. Approval makes this draft eligible "
        "for the delivery tab, where explicit confirmation is still required."
    )
    if draft["review_reasons"]:
        st.caption(
            "Review required: " + ", ".join(draft["review_reasons"])
        )
    edited_text = st.text_area(
        "Draft text",
        value=draft["draft_text"],
        height=260,
        key=f"followup_text_{draft_id}",
        disabled=not editable,
    )
    save_column, approve_column, skip_column = st.columns(3)
    if save_column.button(
        "Save edits",
        key=f"followup_save_{draft_id}",
        disabled=not editable,
    ):
        try:
            db.update_followup_draft_text(draft_id, edited_text)
            _audit(actor, "followup_draft_edited", "followup_draft", draft_id)
        except (ValueError, KeyError) as exc:
            st.warning(str(exc))
        except Exception as exc:
            st.error(f"Could not save draft: {exc}")
        else:
            st.session_state["followup_flash"] = (
                "success",
                f"Draft #{draft_id} edits saved. No message was sent.",
            )
            st.rerun()

    problems = db.get_followup_approval_problems(draft_id)
    can_approve = draft["status"] == "DRAFT" and not problems
    confirmed = approve_column.checkbox(
        "Confirm approval (no send occurs)",
        key=f"followup_confirm_{draft_id}",
        disabled=not can_approve,
    )
    if approve_column.button(
        "Approve for delivery review",
        key=f"followup_approve_{draft_id}",
        disabled=not can_approve or not confirmed,
        type="primary",
    ):
        try:
            db.approve_followup_draft(draft_id, confirmed=confirmed)
            _audit(actor, "followup_draft_approved", "followup_draft", draft_id)
        except (ValueError, KeyError) as exc:
            st.warning(str(exc))
        except Exception as exc:
            st.error(f"Approval failed: {exc}")
        else:
            st.session_state["followup_flash"] = (
                "success",
                f"Draft #{draft_id} APPROVED for delivery review. Human "
                "review is recorded. No message has been sent. Live delivery "
                "is disabled; the delivery tab is mock-only.",
            )
            st.rerun()

    if skip_column.button(
        "Skip draft",
        key=f"followup_skip_{draft_id}",
        disabled=draft["status"] == "SKIPPED",
    ):
        try:
            db.skip_followup_draft(draft_id)
            _audit(actor, "followup_draft_skipped", "followup_draft", draft_id)
        except (ValueError, KeyError) as exc:
            st.warning(str(exc))
        except Exception as exc:
            st.error(f"Could not skip draft: {exc}")
        else:
            st.session_state["followup_flash"] = (
                "info",
                f"Draft #{draft_id} marked SKIPPED. No message was sent.",
            )
            st.rerun()

    if draft["status"] in ("DRAFT", "NEEDS_REVIEW"):
        if problems:
            st.caption("Approval disabled: " + ", ".join(problems))
        elif not draft["channel_available"]:
            st.caption(
                "Approval disabled: no delivery channel (no phone number "
                "on file)."
            )
    st.markdown("**Supporting invoices**")
    st.dataframe(
        db.get_followup_draft_invoices(draft_id),
        alt="Invoices supporting the selected follow-up draft",
        hide_index=True,
    )
    st.caption(
        f"Total taxable value: Rs "
        f"{db.format_paise(draft['total_taxable_value_paise'])} · Total GST: "
        f"Rs {db.format_paise(draft['total_gst_paise'])}. Inclusion reason: "
        f"{draft['inclusion_reason']}."
    )
    with st.expander("Draft status history"):
        st.dataframe(
            db.get_followup_draft_events(draft_id),
            alt="Audit history for the selected follow-up draft",
            hide_index=True,
        )


def _render_later_statement(actor: str) -> None:
    try:
        verification_drafts = [
            row for row in db.get_followup_drafts()
            if row["Status"] == "APPROVED"
        ]
        verification_imports = db.get_gstr2b_imports()
    except Exception as exc:
        st.error(f"Could not load verification data: {exc}")
        return
    st.caption(
        "Compare an approved follow-up with a separate imported batch. "
        "Evidence is appended; invoices, prior runs, and draft status stay "
        "unchanged. A missing row does not prove supplier non-filing."
    )
    if not verification_drafts:
        st.info("Approve a follow-up draft before checking a later statement.")
        return
    if len(verification_imports) < 2:
        st.info(
            "Import the later GSTR-2B batch first. The original follow-up "
            "batch cannot be selected as later evidence."
        )
        return
    verify_draft_labels = {
        f"Draft #{row['Draft']} — {row['Supplier']} "
        f"(original batch #{row['Batch']})": row
        for row in verification_drafts
    }
    verify_draft_label = st.selectbox(
        "Approved follow-up to check",
        options=list(verify_draft_labels),
        key="later_verify_draft",
    )
    verify_draft = verify_draft_labels[verify_draft_label]
    later_import_options = [
        row for row in verification_imports
        if row["Batch"] != verify_draft["Batch"]
    ]
    if not later_import_options:
        st.info("Import a separate batch for later-statement evidence.")
        return
    later_import_labels = {
        f"Batch #{row['Batch']} — {row['File']} "
        f"({row['Accepted']}/{row['Rows']} accepted)": row["Batch"]
        for row in later_import_options
    }
    later_import_label = st.selectbox(
        "Later GSTR-2B batch",
        options=list(later_import_labels),
        key="later_verify_import",
    )
    if st.button("Record later-statement check", key="later_verify_run",
                 type="primary"):
        try:
            db.verify_followup_with_later_import(
                verify_draft["Draft"],
                later_import_labels[later_import_label],
            )
            _audit(
                actor, "later_statement_checked", "followup_draft",
                verify_draft["Draft"],
                later_import_id=later_import_labels[later_import_label],
            )
        except (KeyError, ValueError) as exc:
            st.warning(str(exc))
        except Exception as exc:
            st.error(f"Later-statement check failed: {exc}")
        else:
            st.success(
                "Evidence check recorded. Source invoices, prior "
                "reconciliation results, and follow-up status were not changed."
            )
    try:
        verification_history = db.get_followup_verifications()
    except Exception as exc:
        st.error(f"Could not load later-statement history: {exc}")
        return
    if verification_history:
        st.dataframe([
            {
                "Draft": row["followup_draft_id"],
                "Invoice #": row["invoice_id"],
                "Later batch": row["later_import_id"],
                "File": row["later_file_name"],
                "Outcome": row["outcome"],
                "Amount difference (Rs)": (
                    "" if row["amount_difference_paise"] is None
                    else db.format_paise(row["amount_difference_paise"])
                ),
                "Evidence": row["reason"],
                "Recorded": row["created_at"],
            }
            for row in verification_history
        ], alt="Audited later-statement checks for approved follow-ups",
            hide_index=True)
    else:
        st.info("No later-statement checks recorded yet.")


def _render_delivery(actor: str) -> None:
    st.caption(
        "Inspect delivery evidence or run a simulated send. Live WhatsApp "
        "delivery is disabled and approval alone sends nothing."
    )
    _flash("delivery_flash")
    mode = st.radio(
        "Delivery mode",
        options=["Simulated (mock)", "WhatsApp Cloud API (real)"],
        index=0,
        horizontal=True,
        key="delivery_mode",
        help=(
            "Simulated mode creates a clearly labelled mock delivery record "
            "and never uses the network. Live mode is disabled in this build."
        ),
    )
    real_mode = mode.startswith("WhatsApp")
    if real_mode:
        st.warning(
            "Real delivery is disabled in this build. Environment settings "
            "cannot enable outbound WhatsApp requests. Nothing was sent."
        )
    else:
        st.info(
            "Simulated mode is active. Results are labelled mock and no "
            "external message is sent."
        )
    try:
        all_followup_drafts = db.get_followup_drafts()
        all_deliveries = db.get_message_deliveries(limit=100)
    except Exception as exc:
        st.error(f"Could not load delivery data: {exc}")
        return
    approved_drafts = [
        row for row in all_followup_drafts if row["Status"] == "APPROVED"
    ]
    if not approved_drafts:
        st.info(
            "No approved follow-up drafts yet. Generate drafts and approve "
            "one in the Draft review tab; approval alone sends nothing."
        )
    else:
        approved_labels = {
            f"#{row['Draft']} — {row['Supplier']} (batch "
            f"#{row['Batch']}, run #{row['Run']})": row["Draft"]
            for row in approved_drafts
        }
        approved_label = st.selectbox(
            "Approved draft to preview or simulate",
            options=list(approved_labels),
            key="delivery_draft",
        )
        delivery_draft_id = approved_labels[approved_label]
        delivery_draft = db.get_followup_draft(delivery_draft_id)
        if delivery_draft is not None:
            eligibility = db.get_delivery_eligibility(delivery_draft_id)
            blocking_delivery = db.get_blocking_delivery_for_draft(
                delivery_draft_id
            )
            existing_deliveries = db.get_message_deliveries(
                draft_id=delivery_draft_id, limit=1
            )
            st.markdown(
                f"**Draft #{delivery_draft['id']} — "
                f"{delivery_draft['supplier_name']}**  \n"
                f"Approved: {delivery_draft['approved_at'] or 'unknown'} · "
                f"Recipient snapshot: "
                f"`{delivery_draft['approved_recipient_phone'] or 'missing'}`"
            )
            st.caption(
                "Frozen approved message (a later edit cannot change it):"
            )
            st.text(delivery_draft["approved_text"] or "[snapshot missing]")
            if not eligibility["eligible"]:
                st.warning(
                    "Delivery blocked: " + ", ".join(eligibility["problems"])
                    + ". No message can be sent."
                )
            elif blocking_delivery is not None:
                st.info(
                    f"Delivery #{blocking_delivery['id']} already exists for "
                    f"this draft with status "
                    f"`{blocking_delivery['status']}`. Duplicate sends are "
                    "blocked; use the delivery history below."
                )
            elif existing_deliveries:
                existing_delivery = existing_deliveries[0]
                st.info(
                    f"Delivery #{existing_delivery['id']} is already on "
                    f"record with status `{existing_delivery['status']}`. "
                    "MILAAN keeps one delivery record per approved snapshot "
                    "and does not create a duplicate send."
                )
            elif real_mode:
                st.warning(
                    "Real delivery is disabled in this build. Nothing was sent."
                )
            else:
                send_provider = delivery.MockDeliveryProvider()
                confirmed_send = st.checkbox(
                    "I confirm the recipient consented and want to create "
                    "this simulated delivery.",
                    key=f"delivery_confirm_{delivery_draft_id}",
                )
                if st.button(
                    "Create simulated delivery",
                    key=f"delivery_send_{delivery_draft_id}",
                    disabled=not confirmed_send,
                    type="primary",
                ):
                    try:
                        record = delivery.deliver_approved_draft(
                            delivery_draft_id, send_provider
                        )
                    except (
                        delivery.DeliveryBlockedError,
                        delivery.DeliveryConfigurationError,
                    ) as exc:
                        st.warning(str(exc))
                    except Exception as exc:
                        st.error(f"Delivery attempt failed: {exc}")
                    else:
                        st.session_state["delivery_flash"] = (
                            "success" if record["status"] == "SENT"
                            else "warning",
                            f"Simulated delivery #{record['id']} recorded "
                            f"status {record['status']} with attempt "
                            f"{record['attempt_count']}. No external message "
                            "was sent (mock).",
                        )
                        st.rerun()

    st.markdown("**Delivery history**")
    if all_deliveries:
        st.dataframe([
            {
                "Delivery": row["id"],
                "Draft": row["followup_draft_id"],
                "Batch": row["gstr2b_import_id"],
                "Supplier": row["supplier_name"],
                "Mode": "simulated" if row["provider"] == "mock" else "real",
                "Provider": row["provider"],
                "Status": row["status"],
                "Attempts": f"{row['attempt_count']}/{row['max_attempts']}",
                "Provider message ID": row["provider_message_id"] or "",
                "Next attempt": row["next_attempt_at"] or "",
                "Updated": row["updated_at"],
                "Last error": " · ".join(
                    part for part in (
                        row["last_error_code"], row["last_error_message"]
                    ) if part
                ),
            }
            for row in all_deliveries
        ], alt="Delivery attempts with status and safe provider details",
            hide_index=True)
    else:
        st.caption("No delivery attempts recorded yet.")
    st.caption(
        "Webhook status updates are deferred: DELIVERED/READ require an "
        "authenticated Meta webhook, which this build does not implement."
    )


def page_suppliers() -> None:
    _page_shell(
        "Suppliers",
        "Supplier records, GSTINs, contact details on file, and invoice "
        "counts. GSTINs are structurally validated only.",
        "Records",
    )
    try:
        suppliers = db.get_suppliers()
        invoices = db.get_invoices()
    except Exception as exc:
        st.error(f"Database error: {exc}")
        return
    if not suppliers:
        st.info("No suppliers yet. Import purchase invoices to create them.")
        return
    counts = {}
    for invoice in invoices:
        counts[invoice["Supplier"]] = counts.get(invoice["Supplier"], 0) + 1
    rows = []
    for supplier in suppliers:
        name = supplier["Supplier"]
        rows.append({
            **supplier,
            "Invoices": counts.get(name, 0),
        })
    st.dataframe(
        rows,
        alt="Suppliers with GSTIN, contact details, and invoice counts",
        hide_index=True,
    )
    selected = st.selectbox(
        "Inspect supplier invoices",
        options=[row["Supplier"] for row in suppliers],
        key="supplier_detail",
    )
    supplier_rows = [row for row in invoices if row["Supplier"] == selected]
    if supplier_rows:
        st.dataframe(
            supplier_rows,
            alt=f"Invoices recorded for {selected}",
            hide_index=True,
        )
    else:
        st.caption("No invoices recorded for this supplier.")


def page_audit_log() -> None:
    _page_shell(
        "Audit log",
        "Operational events, draft status changes, delivery evidence, and "
        "later-statement checks. Audit entries never replace source data.",
        "Records",
    )
    operational_tab, draft_tab, delivery_tab, verification_tab = st.tabs(
        ["Operational events", "Draft events", "Delivery evidence",
         "Later statements"]
    )
    with operational_tab:
        try:
            events = db.get_system_audit_events(limit=200)
        except Exception as exc:
            st.error(f"Could not load operational audit: {exc}")
        else:
            if events:
                st.dataframe(
                    events,
                    alt="Recent operational audit events",
                    hide_index=True,
                )
            else:
                st.info("No operational events recorded yet.")
    with draft_tab:
        try:
            events = db.get_all_followup_draft_events(limit=200)
        except Exception as exc:
            st.error(f"Could not load draft events: {exc}")
        else:
            if events:
                st.dataframe(
                    events,
                    alt="Follow-up draft status history",
                    hide_index=True,
                )
            else:
                st.info("No draft status events recorded yet.")
    with delivery_tab:
        try:
            deliveries = db.get_message_deliveries(limit=200)
        except Exception as exc:
            st.error(f"Could not load delivery evidence: {exc}")
        else:
            if deliveries:
                st.dataframe(
                    deliveries,
                    alt="Delivery evidence records",
                    hide_index=True,
                )
            else:
                st.info("No delivery records yet.")
    with verification_tab:
        try:
            verifications = db.get_followup_verifications()
        except Exception as exc:
            st.error(f"Could not load later-statement evidence: {exc}")
        else:
            if verifications:
                st.dataframe(
                    verifications,
                    alt="Later-statement verification evidence",
                    hide_index=True,
                )
            else:
                st.info("No later-statement checks recorded yet.")


def page_settings() -> None:
    _page_shell(
        "Settings",
        "Workspace limits, demo controls, and delivery safety. No setting "
        "can enable live WhatsApp delivery in this build.",
        "Administration",
    )
    _flash("settings_flash")
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.subheader("Workspace")
            st.caption(f"Database: `{db.get_database_path().resolve()}`")
            st.caption("Data: synthetic demo records only.")
            st.caption(
                "GSTIN checks are structural. Live GST registration is not "
                "queried."
            )
            st.caption(
                "Matching: exact GSTIN + invoice + period keys. Ambiguous "
                "evidence is routed to human review."
            )
        with st.container(border=True):
            st.subheader("Demo workspace")
            st.caption(
                "Load or refresh the deterministic demo pipeline. Existing "
                "records are not modified; repeated loads reuse the same "
                "GSTR-2B batch and reconciliation run."
            )
            if st.button("Load demo workspace", key="seed_demo_settings",
                         type="primary"):
                try:
                    result = demo_workflow.load_demo_workspace()
                    _audit(
                        "local-demo", "demo_seeded", "database",
                        db.get_database_path(), **result,
                    )
                except Exception as exc:
                    st.error(f"Demo workspace failed: {exc}")
                else:
                    st.session_state["settings_flash"] = (
                        "success",
                        "Demo workspace ready. No message was sent.",
                    )
                    st.rerun()
    with right:
        with st.container(border=True):
            st.subheader("Delivery")
            st.caption(
                "Live WhatsApp delivery is disabled. Approval records human "
                "review and never sends a message. Simulated delivery is "
                "available for workflow testing and makes no network request."
            )
            st.caption(
                "A phone number on file does not prove consent or ownership."
            )
        with st.container(border=True):
            st.subheader("Validation")
            st.caption(
                "Checks GSTIN format + check digit, required fields, tax "
                "arithmetic (integer paise), intra- vs inter-state structure, "
                "dates, periods, and duplicate natural keys."
            )
            st.caption(
                "A valid result does not determine ITC eligibility. Invalid "
                "and needs-review invoices are excluded from reconciliation."
            )
        with st.container(border=True):
            st.subheader("Audit")
            st.caption(
                "Actions are recorded in the operational audit log. Delivery "
                "evidence is append-only and provider outcomes are never "
                "guessed."
            )


def _configure_page(title: str = "MILAAN · ITC operations") -> None:
    st.set_page_config(
        page_title=title,
        page_icon=":material/account_balance:",
        layout="wide",
    )


def main() -> None:
    _configure_page()
    st.logo("assets/milaan-logo.svg", size="large")
    navigation = st.navigation({
        "Overview": [
            st.Page(
                "pages/1_dashboard.py",
                title="Dashboard",
                icon=":material/space_dashboard:",
                default=True,
            ),
            st.Page(
                "pages/0_agent.py",
                title="WhatsApp agent",
                icon=":material/forum:",
                url_path="agent",
            ),
        ],
        "Workflow": [
            st.Page(
                "pages/2_purchase_invoices.py",
                title="Purchase invoices",
                icon=":material/receipt_long:",
                url_path="purchase-invoices",
            ),
            st.Page(
                "pages/3_gstr2b_import.py",
                title="GSTR-2B import",
                icon=":material/upload_file:",
                url_path="gstr2b-import",
            ),
            st.Page(
                "pages/4_reconciliation.py",
                title="Reconciliation",
                icon=":material/compare_arrows:",
                url_path="reconciliation",
            ),
            st.Page(
                "pages/5_mismatches.py",
                title="Mismatches",
                icon=":material/rule:",
                url_path="mismatches",
            ),
            st.Page(
                "pages/6_followups.py",
                title="Supplier follow-ups",
                icon=":material/draft:",
                url_path="follow-ups",
            ),
        ],
        "Records": [
            st.Page(
                "pages/7_suppliers.py",
                title="Suppliers",
                icon=":material/group:",
                url_path="suppliers",
            ),
            st.Page(
                "pages/8_audit_log.py",
                title="Audit log",
                icon=":material/history:",
                url_path="audit-log",
            ),
        ],
        "Administration": [
            st.Page(
                "pages/9_settings.py",
                title="Settings",
                icon=":material/settings:",
                url_path="settings",
            ),
        ],
    })
    navigation.run()


if __name__ == "__main__":
    main()
