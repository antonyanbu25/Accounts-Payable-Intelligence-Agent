"""
Streamlit frontend for the Accounts Payable Intelligence Agent.

This is a thin client: all reasoning happens in n8n + the FastAPI service.
This app only renders the chat, the UC2 input surface, and a read-only view
of the mock data -- it never computes anything itself.
"""
import csv
import html
import io
import json
import os
import re
from typing import Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import requests
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

N8N_BASE = os.environ.get("N8N_BASE_URL", "http://localhost:5678")
DATABASE_URL = os.environ["DATABASE_URL"]

# Last N user+assistant exchanges sent to Parse Intent as context, so a
# follow-up like "how much do we owe them?" can resolve "them" against the
# vendor named a turn or two earlier. Capped rather than sending the whole
# conversation -- reference resolution essentially never needs more than
# 1-2 turns back, and a fixed cap bounds token cost regardless of how long
# the conversation grows.
HISTORY_TURNS = 3

st.set_page_config(page_title="AP Intelligence Agent", page_icon="📒", layout="wide")

# ---------------------------------------------------------------------------
# Global styling. Targets Streamlit's data-testid attributes directly, which
# is why streamlit is version-pinned in requirements.txt -- these attribute
# names have changed across major Streamlit releases before. Verified
# against the actual rendered DOM of this deployment (not guessed) before
# writing these selectors.
# ---------------------------------------------------------------------------
st.markdown("""
<style>
/* ===========================================================================
   Dark-glow restyle -- design suggestions/validate-payment-advice-build-
   prompt.md + AP Agent Dark Glow Mockup.html. Visual/CSS layer only: no
   change to webhook calls, computed values, or which fields exist.
   Tokens match the approved mockup's palette exactly. --muted is #96969f
   specifically (not darker) -- an earlier pass was flagged as too
   low-contrast to read comfortably against #0a0a0d.

   Inter is loaded via @import (not separate <link> tags before this
   <style> block -- confirmed live that broke Streamlit's markdown
   sanitizer, causing the whole stylesheet to render as literal page text
   instead of being applied). */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

:root {
    --bg: #0a0a0d;
    --panel: #131316;
    --panel-border: #1c1c20;
    --card: #111114;
    --input-bg: #17171b;
    --input-border: #26262b;
    --text: #e8e8ea;
    --text-hero: #f2f2f4;
    --muted: #96969f;
    --accent: #5b8fd9;
    --success-bg: #10261c; --success-border: #1f4a34; --success-text: #6fbf8f;
    --error-bg: #2a1614;   --error-border: #4a2620;   --error-text: #d97a6c;
    --warn-bg: #241a0d;    --warn-border: #4a3416;    --warn-text: #eab876;
    --info-bg: #10202a;    --info-border: #1f3d4e;    --info-text: #7fb8d9;
}

html, body, [data-testid="stAppViewContainer"] {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.mono, [data-testid="stMetricValue"] {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-variant-numeric: tabular-nums;
}

/* layout="wide" removes Streamlit's own max-width; re-cap it here so
   content reads as a centered column on a wide viewer instead of running
   edge-to-edge. 1200px kept from an earlier live-tested decision -- Browse
   Mock Data's tables (6 of them now) need the room a narrower cap would
   take away. Streamlit's own default padding here is ~96px top / 160px
   bottom (confirmed live via computed style) -- reserved for a header bar
   that's empty and transparent in this app (toolbarMode="minimal" leaves
   nothing visible in it), so that space is pure dead air above the tab
   bar. Overridden to match the mockup's own frame padding (28px top).
   Bottom padding is separately responsible for the sticky chat input
   never quite reaching the true bottom of the viewport -- position:sticky
   can't stick past its containing block's own padding, so a large
   trailing padding here holds the input up above the visual bottom edge.
   Reduced to a small buffer instead. */
[data-testid="stMainBlockContainer"] {
    max-width: 1200px;
    margin: 0 auto;
    /* 12px, not 28px -- the vertical block's own 16px inter-element gap
       still applies between the invisible st.markdown(css) call (a real,
       height:0 sibling) and st.tabs() (the first visible widget), adding
       16px on top of whatever's set here. 12 + 16 = 28px total, matching
       the mockup's frame padding -- confirmed live via computed rects,
       not assumed. */
    padding-top: 12px !important;
    padding-bottom: 8px !important;
}

/* Headings/captions read as left-aligned prose in the mockup (section-sub,
   upload helper text, form warnings, not-found captions) -- only the
   landing hero is centered, and it's a raw styled <p>, not a native
   heading/caption component, so it's unaffected by removing this. */
[data-testid="stCaptionContainer"] { color: var(--muted); opacity: 1; }

/* Tab bar: full-contrast labels for BOTH states (mockup: only the colored
   underline should distinguish active, not a dimmed/undimmed contrast
   difference -- Streamlit's default already renders inactive tabs dimmed,
   so this overrides that). Visually reordered to Browse / Ask / Validate
   via flex `order` -- the underlying tab DOM order (and therefore which
   tab loads by default) stays Ask / Validate / Browse; Streamlit's
   st.tabs() has no separate "default" setting independent of list order,
   so reordering the Python call would make Browse load first. `order` on
   a flex child reorders the visual position only. */
[data-testid="stTabs"] [role="tablist"] {
    justify-content: center;
    gap: 4px;
}
/* Streamlit renders its OWN divider line immediately below the tablist --
   a separate, native [data-baseweb="tab-border"] element, not part of
   [role="tablist"] at all -- confirmed live via computed style/DOM
   inspection. Giving the tablist its own border-bottom (the prior
   approach) created a SECOND, visually separate line once a margin-bottom
   was added to push content down, since that margin pushed the native
   line away from the custom one instead of the two coinciding. Fixed by
   removing the custom border entirely and using Streamlit's own line as
   the single divider, moving the breathing-room margin to after it
   instead (see [data-baseweb="tab-panel"] below). */
[data-baseweb="tab-border"] {
    background: var(--panel-border) !important;
}
[data-baseweb="tab-panel"] {
    padding-top: 36px !important;
}
[data-testid="stTabs"] [role="tab"] {
    color: var(--text-hero) !important;
    font-weight: 600;
    opacity: 1 !important;
    padding: 10px 18px !important;
    font-size: 14.5px !important;
    /* Reserved on EVERY tab (not just the active one) so the active tab's
       colored underline below doesn't shift layout when selection changes,
       and so inactive tabs get real breathing room instead of crowding
       together -- this, combined with the padding above, was the actual
       cause of the tab bar reading as one unreadable run-on line. */
    border-bottom: 3px solid transparent !important;
    user-select: none;
}
[data-testid="stTabs"] [role="tab"]:nth-child(1) { order: 2; } /* Ask a Question */
[data-testid="stTabs"] [role="tab"]:nth-child(2) { order: 3; } /* Validate a Payment Advice */
[data-testid="stTabs"] [role="tab"]:nth-child(3) { order: 1; } /* Browse Mock Data */
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    border-bottom-color: var(--accent) !important;
}

/* Flat panels everywhere -- the glow is reserved for actionable surfaces
   (chat input, the Validate button) and must never appear on read-only or
   compliance-critical content; box-shadow "elevation" (the light-theme
   treatment) is replaced with a hairline border on a slightly-lifted flat
   fill instead. stMetric is deliberately NOT in this group -- the mockup
   wants the evidence-panel metrics borderless (label above value, one
   hairline divider above the whole row -- see the explicit divider in
   render_evidence()), not boxed cards. stForm is ALSO deliberately not in
   this group -- the mockup's manual-entry form has no enclosing card at
   all, fields sit directly on the page (see st.form(..., border=False) in
   the Manual Form tab). */
[data-testid="stAlertContainer"],
[data-testid="stExpander"], [data-testid="stTableStyledTable"] {
    background-color: var(--panel) !important;
    border: 1px solid var(--panel-border) !important;
    box-shadow: none !important;
    border-radius: 8px;
}

/* st.radio (Input method / existing-vs-draft toggle / GST split): native
   radio dots kept as-is, a full custom dot-toggle redesign is out of scope
   -- just dark-theme the option-label text so it doesn't look like
   unstyled default Streamlit chrome. */
[data-testid="stRadio"] [role="radiogroup"] {
    gap: 4px 22px;
}
[data-testid="stRadio"] label {
    color: var(--muted) !important;
}
[data-testid="stRadio"] label [data-testid="stMarkdownContainer"] p {
    font-size: 13.5px !important;
}
[data-testid="stRadio"] label:has(input:checked) {
    color: var(--text) !important;
}
[data-testid="stMetric"] { background: transparent !important; border: none !important; box-shadow: none !important; padding: 0 !important; overflow: visible !important; }
[data-testid="stMetricLabel"] { color: var(--muted) !important; font-size: 12px !important; }
/* Streamlit's default metric-value font-size truncates with an ellipsis in
   a 4-column row this narrow (confirmed live: "₹200,0…") -- sized down and
   no-wrap disabled so the full rupee figure is always legible, matching
   the mockup's smaller (~17px) metric values. */
[data-testid="stMetricValue"] {
    color: var(--text-hero) !important;
    font-size: 17px !important;
    white-space: normal !important;
    overflow: visible !important;
    text-overflow: clip !important;
}

/* Suggestion chips: lightweight -- no fill, thin border, muted text. The
   card-shadow treatment (metrics/alerts/tables above) reads as "act here";
   these are examples to skim, not actions, so they get the lightest
   possible treatment in the whole system. Sizing matches the mockup's
   .chip exactly (13px text, 7px/14px padding) -- this is the only
   secondary-type button in the app (every other st.button() call is
   type="primary"), confirmed via grep, so this selector is safe to fully
   customize without touching anything else. */
[data-testid="stBaseButton-secondary"] {
    border-radius: 999px !important;
    border: 1px solid var(--panel-border) !important;
    box-shadow: none !important;
    background-color: transparent !important;
    color: var(--muted) !important;
    font-size: 13px !important;
    padding: 7px 14px !important;
    white-space: nowrap !important;
    min-height: 0 !important;
}
[data-testid="stBaseButton-secondary"]:hover {
    border-color: var(--accent) !important;
    color: var(--text) !important;
}

/* Chat input: two real placements, matching the mockup's actual two-DOM-
   element architecture -- one call inside st.container(key="chat_landing")
   (glow, vertically centered, the one obvious thing to do on an empty
   page), one call after the conversation (flat, sticky-bottom bar). Same
   key="chat_input_widget" on both Python calls preserves widget identity
   across the transition. Base rule here is flat/glow-free; the landing-
   scoped override below adds the glow only in that state. */
/* position: fixed (not sticky) -- sticky only pins once its content
   overflows the viewport and you scroll past its natural flow position;
   a SHORT answer (a refusal, an ineligible/one-line result) never
   generates enough scroll for that to kick in, so the input just sits at
   its ordinary in-flow position -- right after .st-key-chat_conversation's
   150px trailing padding -- leaving a large gap below it down to the true
   viewport bottom. Confirmed live: exactly the "went to the bottom and
   then came back up" behavior reported, reproduced with a short refusal
   answer. Fixed is unconditional (viewport-relative, not scroll-container-
   relative), so it's always flush to the bottom regardless of content
   height -- and as a side effect, it's also always on-screen without
   needing to scroll for it, since a fixed element isn't part of the
   scrollable flow at all. left/right:0 + the existing auto margins center
   it the same way whether the viewport is wide or the 1200px content
   column is narrower than it. */
[data-testid="stChatInput"] {
    background: var(--input-bg) !important;
    border: 1px solid var(--input-border) !important;
    border-radius: 28px;
    min-height: 60px;
    max-width: 720px;
    margin: 0 auto;
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 10;
    /* This is a flex row with exactly one direct child (the wrapper that
       holds textarea+button), but its native align-items is "normal" --
       it does NOT stretch that child to fill the 60px box, so whatever
       height the nested content happens to compute to (itself unstable,
       see the textarea's own explicit-height fix below) sits flush to the
       TOP of the box instead of centered, throwing the send icon visibly
       off-center. Centering here re-centers that single child regardless
       of its own height, independent of the nested-height instability. */
    align-items: center;
}
/* Landing state: back to normal flow (not fixed) so it stays embedded in
   the vertically-centered flex column alongside the hero and chips,
   instead of being ripped out to float at the viewport bottom. */
.st-key-chat_landing [data-testid="stChatInput"] {
    position: static;
    margin: 12px auto 0;
    /* Plain rgba() (not color-mix(in oklab, ...)) -- both express the exact
       same color/opacity (mixing a color with transparent at X% IS just
       that color at X% alpha), but oklab()/color-mix() support is newer
       and less universal; rgba() renders identically everywhere. --accent
       #5b8fd9 = rgb(91,143,217). */
    background:
      radial-gradient(circle at 50% 0%, rgba(91, 143, 217, 0.35) 0%, rgba(91, 143, 217, 0.12) 35%, transparent 70%),
      var(--input-bg) !important;
}
[data-testid="stChatInputTextArea"] {
    font-size: 1.05rem;
    padding-top: 16px;
    padding-bottom: 16px;
    background-color: transparent !important;
    color: var(--text) !important;
    /* Explicit height, not just a max-height cap on an auto height -- the
       textarea's native auto-resize (height:auto, browser-computed from
       content/rows) normally lands on 57px here, but confirmed live that
       once this specific instance sits inside the position:fixed chat-
       state input (not the landing one, which stays in normal flow) and
       has gone through one real type-then-clear cycle, the browser's
       recomputed auto height drops to 40px instead of springing back to
       57 -- no inline style involved, a pure browser reflow quirk tied to
       the fixed positioning context. An explicit height sidesteps the
       auto-calculation entirely so there's nothing for it to get wrong.
       57px matches the value the auto-calc itself produces in the good
       case, so nothing about the visual size changes -- only its
       reliability does. */
    height: 57px !important;
    max-height: 57px !important;
}
[data-testid="stChatInputTextArea"]::placeholder { color: var(--muted) !important; }
/* Cross-version layout fix: confirmed live that the DEPLOYED Streamlit
   (1.62.0, per frontend/requirements.txt) nests stChatInput's internal
   wrapper divs differently from the locally-installed 1.50.0 this CSS was
   built and screenshotted against. Locally, stChatInput's one direct
   child is already a row-direction flex box whose children (the textarea
   and the button) sit side by side, filling the full 720px width -- so
   the app looked correct in every local check. On 1.62.0, that structure
   is a COLUMN stack instead: a narrow (~227px), non-stretched wrapper
   containing a second narrow wrapper, which itself stacks a "text row"
   ABOVE a "button row" -- a newer chat-input design (textarea on top,
   toolbar below), not the single-row pill this app's design calls for.
   Confirmed via direct DOM/computed-style inspection on the live site:
   this produced exactly the reported symptom (placeholder text wrapping
   to 2 lines, send icon rendering below-center instead of at the right
   edge). Targeting by fixed nesting depth would fix one version and risk
   breaking the other (1.50.0's 3 depth-2 siblings -- text wrapper,
   instructions div, button wrapper -- would each get forced to the same
   width, breaking their side-by-side sizing). Instead, select by
   STRUCTURAL ROLE via :has(), which is version-agnostic: "any wrapper
   div that contains both the textarea and the button somewhere inside it"
   is forced into a full-width row (this matches at whichever depth that
   split actually occurs -- one level on 1.50.0, harmlessly re-asserting
   what's already true there; two levels on 1.62.0, actually fixing it);
   "any wrapper that contains the textarea but NOT the button" is given
   flex-grow so it claims the row's remaining width instead of only its
   own intrinsic content width, letting the already-width:100% textarea
   inside it actually stretch. */
[data-testid="stChatInput"] div:has([data-testid="stChatInputTextArea"]):has([data-testid="stChatInputSubmitButton"]) {
    display: flex !important;
    flex-direction: row !important;
    align-items: center !important;
    width: 100% !important;
}
[data-testid="stChatInput"] div:has([data-testid="stChatInputTextArea"]):not(:has([data-testid="stChatInputSubmitButton"])) {
    flex: 1 1 auto !important;
    width: auto !important;
}
/* Double-border fix (+ glow-visibility fix, found the same way): stChatInput
   nests THREE levels of un-testid'd wrapper divs before reaching the
   textarea (confirmed live by walking the full DOM tree) -- not just one
   direct child as first assumed. The direct child carries Streamlit's own
   native border + :focus-within rule pointing at theme.colors.primary;
   TWO of the three levels (not just the first) carry their own opaque
   solid background-color (Streamlit's own near-black theme color), each
   one sitting on top of the last and completely masking the outer
   element's radial-gradient glow -- fixing only the first level (the
   original fix) left the second, deeper one still painted over it, which
   is why the glow was still barely visible after that first pass. `div`
   here (a plain descendant selector, not `> div`) catches every nesting
   level uniformly; it's safe against the textarea and submit button too
   since neither is a `div` element. */
[data-testid="stChatInput"] div,
[data-testid="stChatInput"] div:focus-within {
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
    background: transparent !important;
}
/* Submit-button vertical centering: its flex parent uses
   align-items:flex-end (Streamlit's native default, presumably meant for
   a textarea that grows upward as it wraps to multiple lines), which
   pushed the send icon ~9.5px below true vertical center in this
   single-line pill -- confirmed live via computed rects. */
[data-testid="stChatInputSubmitButton"] {
    align-self: center !important;
}

/* Landing state (no conversation yet): hero + glowing input + suggestion
   chips together as ONE block, vertically centered in the space below the
   tab bar -- set only while st.session_state.messages is empty and no
   question is pending (see the Python st.container(key="chat_landing")
   block, which now wraps the chat_input call too, not just the hero/chips
   above it), harmless once a real conversation exists (never rendered
   again). */
.st-key-chat_landing { min-height: 62vh; display: flex; flex-direction: column; justify-content: center; }
/* !important throughout -- confirmed live that a bare .hero class selector
   LOSES to Streamlit's own [data-testid="stMarkdownContainer"] p rule
   (0,1,1 specificity vs .hero's 0,1,0), silently collapsing this to
   ordinary 16px body-text size. Only bites elements that are literally a
   <p> tag (this one is); div-based classes elsewhere in this file don't
   hit the same collision. */
.hero {
    text-align: center !important;
    font-weight: 300 !important;
    font-size: 32px !important;
    color: var(--text-hero) !important;
    letter-spacing: -0.01em !important;
    margin: 20px 0 28px !important;
}

/* Chat-state conversation column: capped width, a hairline divider above
   the first message, and enough reserved bottom padding that the fixed-
   bottom input (sticky, see above) never overlaps the last message. */
.st-key-chat_conversation {
    max-width: 700px;
    margin: 0 auto;
    border-top: 1px solid var(--panel-border);
    padding-top: 32px;
    padding-bottom: 150px;
}
/* Loading row (the "Looking this up..." spinner): same 700px column, and
   its own bottom clearance for the same reason as chat_conversation above
   -- it renders as a SEPARATE, later element (after chat_input in script
   order, since chat_input is fixed/out-of-flow and doesn't push it down),
   so it doesn't inherit chat_conversation's own padding automatically. */
.st-key-loading_row {
    max-width: 700px;
    margin: 0 auto;
    padding-bottom: 150px;
}

/* Chat messages: user right-aligned as a bubble, agent left-aligned as
   plain text -- deliberately asymmetric, not two bubbles, so the agent's
   longer grounded answers read as the primary content and the user's
   short questions read as prompts. No avatar icons either role. */
[data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] { display: none; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    margin-left: auto;
    max-width: 78%;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 14px;
    padding: 10px 16px;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
    max-width: 82%;
}

/* Restrained, harmonized alert palette for the dark ground. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {
    background-color: var(--success-bg) !important; border: 1px solid var(--success-border) !important;
}
[data-testid="stAlertContentSuccess"] { color: var(--success-text) !important; }

[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {
    background-color: var(--error-bg) !important; border: 1px solid var(--error-border) !important;
}
[data-testid="stAlertContentError"] { color: var(--error-text) !important; }

/* Info = "held for review" (category conflict / tax treatment refused) --
   deliberately calm and blue, NOT error-red or warning-amber, so a
   genuine data conflict with no defensible resolution reads as correct,
   confident behavior rather than a broken state. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {
    background-color: var(--info-bg) !important; border: 1px solid var(--info-border) !important;
}
[data-testid="stAlertContentInfo"] { color: var(--info-text) !important; }

/* "Held for review" as one continuous block (heading + pre-tax ledger
   position + category-conflict detail together) -- used by both UC1's
   tax_treatment_refused branch and UC2's diff.blocked branch, so the same
   underlying condition reads identically in both places. */
.refusal-block {
    background: var(--info-bg);
    border: 1px solid var(--info-border);
    border-radius: 8px;
    padding: 14px 18px;
    margin-top: 16px;
    color: var(--info-text);
    font-size: 13.5px;
    line-height: 1.6;
}
.refusal-block .heading { font-weight: 600; color: var(--info-text); margin-bottom: 4px; }

/* Warning = the "not eligible to pay" / unapplied-advance family --
   deliberately its own color, distinct from both error (numeric mismatch)
   and info (held for review). Never visually confusable with either. */
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {
    background-color: var(--warn-bg) !important; border: 1px solid var(--warn-border) !important;
    border-left: 4px solid var(--warn-text) !important;
}
[data-testid="stAlertContentWarning"] { color: var(--warn-text) !important; font-weight: 600; }

/* The Validate button gets the same radial-glow treatment as the chat
   input -- the one primary action on this tab. Every other button
   (form-submit buttons inside expanders, etc.) is left alone; grep
   confirms this selector only ever matches type="primary" buttons, and
   "Validate this advice" is the only one in regular use on this tab. */
[data-testid="stBaseButton-primary"], [data-testid="stFormSubmitButton"] button {
    /* Plain rgba(), same reasoning as the chat-input glow above. */
    background:
      radial-gradient(circle at 50% 100%, rgba(91, 143, 217, 0.32) 0%, rgba(91, 143, 217, 0.10) 40%, transparent 72%),
      var(--input-bg) !important;
    border: 1px solid var(--input-border) !important;
    color: var(--text-hero) !important;
    font-weight: 600;
}

/* Comparison table (UC2 field-by-field diff): fixed column widths so
   Submitted/Correct/Match don't stretch full-bleed with large dead gaps,
   and those three columns center-aligned against their (also centered)
   headers -- the mismatch between a left-aligned header and a right-
   aligned value column was a real readability bug caught while building
   this mockup. st.table (not st.dataframe) renders real HTML, so this is
   reachable via CSS at all. */
[data-testid="stTable"] table { table-layout: fixed; max-width: 640px; }
[data-testid="stTable"] th:not(:first-child), [data-testid="stTable"] td:not(:first-child) {
    text-align: center !important;
}
/* pandas Styler's .hide(axis="index") has no effect when rendered through
   st.table() -- confirmed live, the row-number column (.row_heading) and
   its blank corner header (.blank) still render regardless. CSS fallback
   since the Styler-level directive doesn't reach the actual output here. */
[data-testid="stTable"] th.row_heading, [data-testid="stTable"] th.blank {
    display: none;
}

/* Browse Mock Data: st.dataframe already provides its own scroll/sticky-
   header behavior -- only the palette needs to follow the theme here. */
[data-testid="stDataFrame"] { background-color: var(--panel); }

/* Per-tab section heading (Validate / Browse) -- distinct from the removed
   app-level st.title(); the Ask tab has no equivalent in the mockup. */
.section-title { font-size: 22px; font-weight: 600; color: var(--text-hero); margin: 0 0 8px; }
.section-sub { color: var(--muted); font-size: 14px; margin-bottom: 24px; max-width: 700px; line-height: 1.5; }

/* Flat, non-interactive JSON preview (replaces st.json()'s default
   collapsible viewer at all 3 call sites) -- hand-colored key/value spans,
   no carets/copy button. */
.example-json {
    background: var(--input-bg);
    border: 1px solid var(--input-border);
    border-radius: 8px;
    padding: 16px 18px;
    font-size: 13px;
    line-height: 1.7;
    color: var(--text);
    margin-bottom: 24px;
    max-width: 620px;
    overflow-x: auto;
}
.example-json .k { color: var(--info-text); }
.example-json .v { color: var(--text-hero); }

/* UC2 match/mismatch verdict: plain inline colored text, no box -- distinct
   from the boxed alert language used everywhere else in the app, and from
   the separate eligibility-warning block below it (still boxed, untouched).
   Real st.markdown() output (see Python) can be a <p>, an <h1>/<h2> if the
   LLM's narration includes a markdown heading, a <ul>, etc. -- the `*`
   descendant selector colors whatever it actually turns out to be, rather
   than assuming it's always one <p>. */
.st-key-verdict_ok, .st-key-verdict_ok * { color: var(--success-text) !important; }
.st-key-verdict_ok { margin-top: 18px; font-size: 14px; }

.st-key-verdict_bad_heading, .st-key-verdict_bad_heading * { color: var(--error-text) !important; }
.st-key-verdict_bad_heading { margin-top: 18px; font-size: 14px; }

.st-key-verdict_bad_text, .st-key-verdict_bad_text * { color: var(--muted) !important; }
.st-key-verdict_bad_text { font-size: 13px; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

tab_ask, tab_validate, tab_browse = st.tabs(
    ["Ask a Question", "Validate a Payment Advice", "Browse Mock Data"]
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _render_json_block(data) -> None:
    """Flat, non-interactive JSON preview matching the mockup's .example-json
    block (hand-colored key/value spans, no collapse UI/carets/copy button)
    -- replaces st.json()'s default viewer at all 3 call sites. A generic
    recursive walker, so currency floats are NOT force-formatted to 2
    decimals here (that would also wrongly reformat non-currency floats,
    e.g. a GST rate percentage) -- Python's own str(float) is used as-is."""
    def esc(s) -> str:
        return html.escape(str(s))

    def render(v, indent: int) -> str:
        pad = "&nbsp;" * (indent * 2)
        if isinstance(v, dict):
            if not v:
                return "{}"
            items = list(v.items())
            lines = [
                f'{pad}&nbsp;&nbsp;<span class="k">"{esc(k)}"</span>: '
                f'{render(val, indent + 1)}{"," if i < len(items) - 1 else ""}'
                for i, (k, val) in enumerate(items)
            ]
            return "{<br>" + "<br>".join(lines) + f"<br>{pad}}}"
        if isinstance(v, list):
            if not v:
                return "[]"
            lines = [
                f'{pad}&nbsp;&nbsp;{render(val, indent + 1)}{"," if i < len(v) - 1 else ""}'
                for i, val in enumerate(v)
            ]
            return "[<br>" + "<br>".join(lines) + f"<br>{pad}]"
        if isinstance(v, str):
            return f'<span class="v">"{esc(v)}"</span>'
        if isinstance(v, bool):
            return f'<span class="v">{"true" if v else "false"}</span>'
        if v is None:
            return '<span class="v">null</span>'
        return f'<span class="v">{esc(v)}</span>'

    st.markdown(f'<div class="example-json mono">{render(data, 0)}</div>', unsafe_allow_html=True)


def render_evidence(evidence: dict, tax_evidence: Optional[dict] = None, compact: bool = False):
    if "invoices" in evidence:
        # Vendor-lookup response (general vendor info, e.g. "which state is
        # this vendor in", "what other invoices do they have") -- NOT a
        # balance/tax calculation, so this renders as a profile + invoice
        # list rather than forcing it through the money-metric layout below,
        # which would show misleading blank placeholders for fields (GST,
        # TDS, net disbursement) that simply don't apply to this response.
        c1, c2 = st.columns(2)
        c1.write(f"**GSTIN:** {evidence.get('gstin', '—')}")
        c1.write(f"**Registered state (GSTIN):** {evidence.get('registered_state', '—')}")
        c1.write(f"**Payment terms:** {evidence.get('payment_terms', '—')}")
        c2.write(f"**Status:** {evidence.get('vendor_status', '—')}")
        c2.write(f"**Onboarding state (portal):** {evidence.get('onboarding_state', '—')}")
        c2.write(f"**Onboarded:** {evidence.get('onboarding_date', '—')} ({evidence.get('onboarding_status', '—')})")
        if (evidence.get("registered_state") and evidence.get("onboarding_state")
                and evidence["registered_state"] != evidence["onboarding_state"]):
            st.warning(f"⚠️ Onboarding state ({evidence['onboarding_state']}) differs from the GSTIN-registered "
                       f"state ({evidence['registered_state']}) — GSTIN is authoritative for tax purposes; "
                       f"see the authority-conflict rule this system applies everywhere else.")
        invoices = evidence.get("invoices") or []
        st.write(f"**Invoices on file ({len(invoices)}):**")
        if invoices:
            st.dataframe(invoices, use_container_width=True, hide_index=True)

    elif evidence.get("tax_treatment_refused"):
        # A genuine, no-defensible-rule source conflict is correct, confident
        # behavior -- not a broken state -- so this deliberately uses the
        # calm info-blue .refusal-block rather than a warning/error. It's a
        # separate elif branch from the normal eligibility display below, so
        # this never collides with or dilutes the "not eligible to pay"
        # warning. Rendered as ONE continuous block (heading + pre-tax
        # position + category-conflict detail together), not two separate
        # Streamlit calls that render as visually distinct sibling elements.
        cc = evidence.get("category_conflict")
        conflict_html = ""
        if cc:
            conflict_html = (
                f' PO says <strong>{html.escape(str(cc["po_category"]))}</strong>, invoice says '
                f'<strong>{html.escape(str(cc["invoice_category"]))}</strong> — no rule to resolve which is '
                f'correct, so tax treatment is withheld pending review. This is a genuine conflict in the '
                f'source data that needs a human decision, not an error in the system.'
            )
        st.markdown(
            f'<div class="refusal-block">'
            f'<div class="heading">⏸ Held for review — {html.escape(str(evidence.get("eligibility", "")))}</div>'
            f'Pre-tax ledger position (base − advances − credits − payments): '
            f'₹{evidence.get("pre_tax_ledger_position"):,.0f}.{conflict_html}'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        gst = evidence.get("gst") or {}
        # A single 4-column row, not two 2x2 rows -- matches the mockup's
        # "4 metrics in one borderless row" layout. The divider is an
        # explicit element (not a CSS rule targeting the column row itself)
        # since Streamlit gives st.columns() no stable hook to distinguish
        # THIS row from every other st.columns() call elsewhere in the app.
        # `compact` (2x2 instead of 1x4) is the one exception: found live
        # that render_comparison_evidence() already halves the page width
        # per invoice before this function ever runs, so cramming all 4
        # metrics into THAT halved width truncated "Net disbursement due"'s
        # value to "₹102..." -- unreadable, not just visually busier. 2x2
        # inside a half-width column gives each metric roughly the same
        # width st.metric() already renders correctly at in the single-
        # invoice (non-compact, full-width, 1x4) case.
        st.markdown('<div style="border-top: 1px solid var(--panel-border); margin: 18px 0 4px;"></div>',
                    unsafe_allow_html=True)
        base_amount_val = f"₹{evidence.get('base_amount', 0):,.0f}"
        gst_val = f"₹{gst.get('gst_amount', 0):,.0f}" if gst else "—"
        tds_val = f"₹{evidence.get('tds_amount', 0):,.0f}" if evidence.get("tds_amount") is not None else "—"
        net_val = f"₹{evidence.get('net_disbursement_due', 0):,.0f}"
        if compact:
            r1c1, r1c2 = st.columns(2)
            r1c1.metric("Base amount", base_amount_val)
            r1c2.metric("GST", gst_val)
            r2c1, r2c2 = st.columns(2)
            r2c1.metric("TDS withheld", tds_val)
            r2c2.metric("Net disbursement due", net_val)
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Base amount", base_amount_val)
            c2.metric("GST", gst_val)
            c3.metric("TDS withheld", tds_val)
            c4.metric("Net disbursement due", net_val)

        if gst.get("split_type") == "IGST":
            st.caption(f"GST split: IGST ₹{gst.get('igst', 0):,.0f} (inter-state — vendor and office are in different states)")
        elif gst.get("split_type") == "UNKNOWN":
            st.caption("GST split: could not be determined — this invoice has no linked purchase order/office "
                       "on file to compare vendor and office state against. Total GST amount above is still correct.")
        elif gst:
            st.caption(f"GST split: CGST ₹{gst.get('cgst', 0):,.0f} + SGST ₹{gst.get('sgst', 0):,.0f} (intra-state)")

        st.write(f"**Payment eligibility:** {evidence.get('eligibility')}")
        if evidence.get("unapplied_advance_advisory"):
            st.info(f"⚠️ Advisory: an unapplied advance of ₹{evidence['unapplied_advance_advisory']:,.0f} "
                     f"exists against this vendor/PO and has NOT been netted here — a possible overpayment risk if missed.")
        twm = evidence.get("three_way_match")
        if twm and not twm.get("matched"):
            st.error(f"⚠️ 3-way match FAILED: PO ₹{twm['po_amount']:,.0f} / receipt ₹{twm['receipt_amount']:,.0f} "
                      f"/ invoice ₹{twm['invoice_base_amount']:,.0f} — difference exceeds tolerance (₹{twm['tolerance']:,.0f}).")

    if tax_evidence:
        with st.expander("Tax document sources (exact clause quoted)"):
            for label, key in [("GST", "gst"), ("TDS", "tds")]:
                sub = tax_evidence.get(key)
                if not sub:
                    continue
                if sub.get("status") == "found":
                    st.markdown(f"**{label}** — *{sub['source_filename']}*")
                    st.markdown(f"> {sub['key_clause']}")
                elif sub.get("status") == "not_found":
                    st.markdown(f"**{label}** — no applicable rule found in the corpus.")

    with st.expander("Full structured evidence (raw)"):
        _render_json_block(evidence)


def render_comparison_evidence(invoices: list):
    """Comparison response ({comparison: true, invoices: [...]}) -- side-by-
    side columns, each rendered with the existing single-invoice
    render_evidence() so the money-metric layout and guard-fallback styling
    stay in exactly one place rather than being reimplemented here."""
    cols = st.columns(len(invoices)) if invoices else []
    for col, inv in zip(cols, invoices):
        with col:
            st.markdown(f"**Invoice {inv.get('invoice_id', '—')}** ({inv.get('invoice_date', '—')})")
            render_evidence(inv.get("evidence") or {}, inv.get("tax_evidence"), compact=True)


# ---------------------------------------------------------------------------
# TAB 1 — UC1: Ask a question
# ---------------------------------------------------------------------------

with tab_ask:
    suggestions = [
        "How much is pending for TechNova Software Solutions on invoice INV-9?",
        "Is TechNova invoice 17 correctly taxed?",
        "What tax treatment applies to Skyline Software Labs' latest invoice?",
        "What's the outstanding balance for Zenith Global Traders?",
    ]
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pending_question" not in st.session_state:
        st.session_state.pending_question = None

    def _use_suggestion(s):
        st.session_state.pending_question = s

    # Suggestions only before a conversation starts -- once there's history,
    # they're just clutter to scroll past, and hiding them shortens the
    # distance between the latest answer and the input (see sticky-input
    # note below). Matches how every real chat product treats starter
    # prompts (e.g. ChatGPT's disappear after the first message).
    #
    # Uses on_click (a real Streamlit callback), not an inline `if
    # button(...)` check -- callbacks run and update session_state BEFORE
    # the script reruns and re-renders from the top, so this condition
    # already sees pending_question set on the very same click, hiding
    # suggestions immediately rather than one interaction later. (An
    # earlier attempt just checked pending_question inline without a
    # callback -- that doesn't work, since the button's own assignment
    # only happens *after* this condition already rendered it, on the
    # same pass.) The one case this still can't cover: typing a question
    # directly into chat_input as literally the first message -- that
    # value isn't known until st.chat_input() itself is called, further
    # down. Harmless and self-correcting from the second question on.
    # Two real states, matching the mockup's actual architecture: landing
    # (hero + glowing input + chips, ALL vertically centered together as one
    # block) vs. chat-state (thread above, flat sticky-bottom input below,
    # no chips). These are an EXHAUSTIVE if/else on the same condition, not
    # two independently-gated blocks -- see the note below on why that
    # matters for the one-frame transitional rerun a suggestion-chip click
    # produces. Both branches call st.chat_input(..., key="chat_input_widget")
    # with the SAME key, so Streamlit preserves the widget's identity across
    # the transition (the two calls never both render in the same run).
    show_landing = not st.session_state.messages and not st.session_state.pending_question

    if show_landing:
        # Vertically centers hero+input+chips together in the space below
        # the tab bar, so the glowing input is the one obvious focal point.
        # st.container(key=...) (not two separate st.markdown('<div>')/
        # ('</div>') calls) -- confirmed live that a raw opening tag from
        # one st.markdown call does NOT actually wrap widgets rendered by
        # later Streamlit calls (each becomes its own sibling element), so
        # the "wrapper" div was empty and the intended min-height just
        # showed up as a big blank gap above the real content.
        # st.container(key="chat_landing") emits a real DOM container
        # Streamlit itself manages, taggable as .st-key-chat_landing.
        with st.container(key="chat_landing"):
            st.markdown('<p class="hero">Ask about a vendor, invoice, or tax treatment</p>',
                        unsafe_allow_html=True)
            typed = st.chat_input("Ask about a vendor balance or tax treatment...",
                                   key="chat_input_widget")
            # st.container(horizontal=True, ...) (Streamlit 1.32+, confirmed
            # present on the pinned version) -- a real flex row that wraps
            # to the next line if chips don't fit, each child sized to its
            # own content. st.columns() (the prior approach) allocates
            # fixed equal-width flex-basis percentages per column instead,
            # which is why chips rendered as large stretched rectangles
            # with wrapped multi-line text instead of the mockup's tight,
            # content-hugging pills -- this is the actual fix, not just a
            # style tweak, since st.columns() has no CSS-reachable "shrink
            # to content" mode.
            with st.container(horizontal=True, horizontal_alignment="center", gap="small"):
                for i, s in enumerate(suggestions):
                    st.button(s, key=f"sugg_{i}", on_click=_use_suggestion, args=(s,))
    else:
        # Message loop BEFORE chat_input in DOM order -- required for
        # sticky-bottom positioning to stay coherent while a tall
        # conversation scrolls (confirmed live). Wrapped in
        # st.container(key="chat_conversation") for the mockup's capped-
        # width, top-divider treatment (see CSS).
        with st.container(key="chat_conversation"):
            for msg in st.session_state.messages:
                with st.chat_message(msg["role"]):
                    st.write(msg["content"])
                    if msg.get("comparison"):
                        render_comparison_evidence(msg.get("invoices") or [])
                    elif msg.get("evidence"):
                        render_evidence(msg["evidence"], msg.get("tax_evidence"))

        if st.session_state.messages:
            # Auto-scroll on every rerun -- the sticky input only stays
            # visible once you've *already* scrolled near it; without this,
            # a conversation taller than the viewport still needs a manual
            # scroll down after every answer. Scrolls to the TOP of the
            # latest user question (not to the raw scrollHeight/absolute
            # bottom, the prior approach) -- once an answer includes the
            # evidence panel and expanders, scrollHeight is often well past
            # where the new turn actually starts, so jumping straight there
            # cut off the question and the opening of the answer (confirmed
            # live). This instead anchors the new turn's start at the top
            # of the viewport, same pattern ChatGPT/Claude.ai use, so the
            # narration reads from its beginning and grows downward toward
            # the input. components.html runs in its own same-origin
            # iframe, so window.parent.document is how it reaches the
            # actual app DOM -- a standard, documented pattern for this
            # exact case. height=0 keeps the iframe itself invisible.
            # <!-- {len(st.session_state.messages)} --> makes the srcdoc
            # textually different on every turn -- confirmed live that
            # without this, the scroll silently stopped firing from the
            # 3rd turn onward: components.html re-embeds an iframe each
            # call, but when its content is byte-identical to the previous
            # render (as this script always was, having no per-render
            # state of its own), the browser doesn't treat the <script>
            # inside it as new and never re-executes it. A trailing HTML
            # comment forces the content to actually differ each time.
            components.html(f"""
                <script>
                setTimeout(function() {{
                    const messages = window.parent.document.querySelectorAll('[data-testid="stChatMessage"]');
                    if (messages.length) {{
                        const anchor = messages.length >= 2 ? messages[messages.length - 2] : messages[0];
                        anchor.scrollIntoView({{block: 'start', behavior: 'auto'}});
                    }}
                }}, 60);
                </script>
                <!-- {len(st.session_state.messages)} -->
            """, height=0)

        # Flat, sticky-bottom bar (no glow -- see CSS, the glow is scoped to
        # .st-key-chat_landing only). Same reasoning as before on why this
        # stays pinned to the bottom rather than moving with the
        # conversation: every real chat product (ChatGPT, Claude.ai,
        # WhatsApp) keeps the composer anchored while the conversation
        # scrolls, not the other way around.
        typed = st.chat_input("Ask about a vendor balance or tax treatment...",
                               key="chat_input_widget")

    question = typed or st.session_state.pending_question
    st.session_state.pending_question = None

    if question:
        # Deliberately NOT rendered live/inline here (no st.chat_message
        # calls in this block) -- this code runs *after* chat_input above,
        # so anything drawn here would land below the sticky input rather
        # than above it, defeating the whole point of pinning it. Instead:
        # process silently, append both turns to session_state, then
        # st.rerun() so the history loop above (which now includes this
        # turn) draws everything in the correct order on the very next
        # pass, with chat_input rendered fresh (and empty) after it again.
        st.session_state.messages.append({"role": "user", "content": question})
        # Wrapped + auto-scrolled for the same reason the fixed input needed
        # position:fixed in the first place: this spinner is the LAST thing
        # in the flow at this exact moment (Q3's own bubble doesn't exist in
        # the DOM yet -- the message loop above already ran before this
        # question was appended to session_state), and the scroll position
        # left over from the PREVIOUS rerun has no reason to already show
        # it. Confirmed live: it was rendering directly behind the fixed,
        # opaque input bar, fully hidden. The container's own bottom padding
        # (see CSS -- matches the fixed input's real footprint) is what
        # actually creates clearance; scrollIntoView(block:'end') then
        # aligns that padding's bottom edge with the viewport bottom, which
        # leaves the visible spinner text sitting just above the bar
        # instead of behind it. The scroll script sits INSIDE the same
        # container, before the spinner -- by the time its setTimeout
        # fires, Streamlit has already rendered the spinner text below it
        # in the same box, so the container's measured height is correct.
        with st.container(key="loading_row"):
            components.html("""
                <script>
                setTimeout(function() {
                    const el = window.parent.document.querySelector('.st-key-loading_row');
                    if (el) { el.scrollIntoView({block: 'end', behavior: 'auto'}); }
                }, 60);
                </script>
            """, height=0)
            with st.spinner("Looking this up across all three sources..."):
                try:
                    # Prior turns only -- exclude the question just appended
                    # above. role/content ONLY, never the evidence/tax_evidence
                    # JSON blobs: the narrative prose already states prior
                    # figures in plain language, and sending the raw structured
                    # evidence would balloon token cost for no resolution
                    # benefit (Parse Intent only needs to know *what* was asked
                    # and answered, not re-derive numbers from it).
                    prior_turns = st.session_state.messages[:-1]
                    history = [{"role": m["role"], "content": m["content"]}
                               for m in prior_turns[-(HISTORY_TURNS * 2):]]
                    resp = requests.post(f"{N8N_BASE}/webhook/uc1-ask",
                                          json={"question": question, "history": history}, timeout=90)
                    data = resp.json()
                    if "narrative" not in data and data.get("message"):
                        # Graceful-refusal paths (nonexistent vendor, out-of-
                        # domain question) return {error, message}, not
                        # {narrative, evidence} -- surface that message
                        # directly instead of falling through to a blank
                        # "(no answer returned)".
                        data = {"narrative": data["message"], "evidence": {}}
                except Exception as e:
                    data = {"narrative": f"Something went wrong reaching the agent: {e}", "evidence": {}}
        st.session_state.messages.append({
            "role": "assistant", "content": data.get("narrative", "(no answer returned)"),
            "evidence": data.get("evidence"), "tax_evidence": data.get("tax_evidence"),
            "comparison": data.get("comparison", False), "invoices": data.get("invoices"),
        })
        st.rerun()


# ---------------------------------------------------------------------------
# TAB 2 — UC2: Validate a payment advice
# ---------------------------------------------------------------------------

# A real payment advice comes out of an accounting system as a spreadsheet/
# CSV export -- nobody hand-writes JSON. This is a deterministic, structured
# parse (same guarantee as the JSON path: no LLM, no OCR, nothing that could
# misread a digit), just of a different structured format. Shared by both
# the "Upload a file" and "Paste JSON or CSV row" modes below so the parsing
# logic lives in exactly one place.
_ADVICE_NUMERIC_FIELDS = ("base_amount", "gst_rate_pct", "gst_cgst", "gst_sgst",
                          "gst_igst", "tds_amount", "net_payable_claimed")

# Same fixed category enum the n8n side uses (build_uc1_workflow.py's
# category_mentioned tool field, build_uc2_workflow.py's HSN/SAC map) --
# a dropdown here instead of free text avoids a typo silently producing a
# false "category doesn't match" or "rate not found" result downstream.
CATEGORY_OPTIONS = ["Furniture", "Software", "Services", "Food", "Appliances"]


@st.cache_data(ttl=60)
def _load_invoice_options():
    """Real invoices, for the Manual Form's invoice picker -- so validating
    an existing invoice never requires knowing or typing an ID up front, or
    tab-hopping to Browse Mock Data to find one. Same columns as that tab's
    Invoices table, PLUS registered_state/office_state -- these two let the
    Manual Form auto-detect CGST+SGST vs. IGST once an invoice is picked,
    instead of asking the user to choose manually. LEFT JOIN through
    purchase_order -> requisition -> office (the same path n8n's own
    Retrieve Facts query already uses) because po_id is nullable -- 1 of
    the 25 seeded invoices genuinely has no PO (non-PO/"maverick" spend,
    confirmed by querying the real data before relying on this), so
    office_state comes back NULL for that one row and the split can't be
    auto-detected for it; the Manual Form falls back to asking directly
    only in that case. Cached briefly since this is read-only reference
    data re-queried on every widget interaction otherwise."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT i.invoice_id, v.legal_name AS vendor, c.category_name AS category,
               i.base_amount, i.status, v.registered_state, o.state AS office_state
        FROM invoice i
        JOIN vendor_master v ON i.vendor_id = v.vendor_id
        JOIN category c ON i.category_id = c.category_id
        LEFT JOIN purchase_order po ON i.po_id = po.po_id
        LEFT JOIN requisition req ON po.requisition_id = req.requisition_id
        LEFT JOIN office o ON req.office_id = o.office_id
        ORDER BY i.invoice_id;
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# Each CSV row is a full, real webhook round-trip (tax lookup, compute,
# diff, LLM narration) run one after another, not in parallel -- an
# unbounded batch would mean an unbounded wait with a spinner and no
# progress feedback beyond "row N of M". Same reasoning as UC1's
# invoice-comparison feature capping itself at 2 invoices.
MAX_CSV_ROWS = 10


def _parse_csv_row_dict(row: dict) -> dict:
    """Converts one csv.DictReader row into the {invoice_id, submitted_advice:
    {...}} shape every other input mode already produces. Only populates
    submitted_advice keys that are actually present and non-empty in the
    row, so e.g. a CGST+SGST row doesn't send an empty gst_igst downstream.
    invoice_id is optional -- an omitted or empty column is a draft advice
    for an invoice that isn't recorded in the system yet, same as the
    Manual Form's "No" toggle."""
    advice = {}
    for key, val in row.items():
        if key is None or key == "invoice_id" or val is None or val.strip() == "":
            continue
        advice[key] = float(val) if key in _ADVICE_NUMERIC_FIELDS else val
    result = {"submitted_advice": advice}
    if row.get("invoice_id"):
        result["invoice_id"] = int(row["invoice_id"])
    return result


def _parse_csv_rows(text: str) -> list:
    """Parses a CSV (header + one or more data rows, e.g. pasted straight
    out of Excel) into a LIST of {invoice_id, submitted_advice: {...}}
    payloads, one per row -- lets a small batch of advices be validated in
    one paste/upload instead of forcing exactly one row at a time. Capped
    at MAX_CSV_ROWS (see its own comment)."""
    stripped_text = text.strip()
    # Auto-detect comma vs. tab -- selecting a range in Excel/Google Sheets
    # and pasting it produces TAB-separated text, not comma-separated, even
    # though it's visually indistinguishable from a real CSV once it lands
    # in the box. csv.DictReader defaults to comma; with no comma anywhere
    # in a tab-separated paste, it swallows the ENTIRE header line into one
    # garbage column name -- confirmed live -- invoice_id silently
    # disappears (falls back to draft mode) and none of the real fields
    # exist at all, which cascades into a confusing downstream error with
    # no useful message at this layer ("Expecting value: line 1 column 1
    # (char 0)"). This is exactly the "copied straight out of a
    # spreadsheet" case the docstring above already promised support for --
    # sniffing the real delimiter first is what actually makes that true.
    first_line = stripped_text.split("\n", 1)[0]
    delimiter = "\t" if "\t" in first_line and "," not in first_line else ","
    reader = csv.DictReader(io.StringIO(stripped_text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise ValueError("No data rows found -- paste a header row plus at least one data row.")
    if len(rows) > MAX_CSV_ROWS:
        raise ValueError(f"Found {len(rows)} data rows -- this validates up to {MAX_CSV_ROWS} at a time. "
                          f"Split into smaller batches.")
    return [_parse_csv_row_dict(row) for row in rows]


FIXTURES = {
    # Plain text labels, deliberately no emoji -- st.selectbox can't render
    # colored/styled glyphs (options are plain strings), so an emoji here
    # only ever shows as a full-color platform icon that visually clashes
    # with the app's restrained, monochrome dark palette. Confirmed live:
    # bright green/red/yellow/amber squares floating in an otherwise muted
    # UI. Matches the app's own design rule elsewhere -- emoji reserved for
    # compact functional status glyphs inside verdict/refusal blocks
    # specifically, not decorative labels.
    "Correct advice (INV-1)": {
        "invoice_id": 1,
        "submitted_advice": {
            "base_amount": 85000.00, "category": "Furniture",
            "gst_rate_pct": 18.0, "gst_igst": 15300.00,
            "tds_amount": 0.00, "net_payable_claimed": 100300.00,
        },
    },
    "Multi-error advice (INV-4)": {
        "invoice_id": 4,
        "submitted_advice": {
            "base_amount": 61000.00, "category": "Appliances",
            "gst_rate_pct": 12.0, "gst_cgst": 3660.00, "gst_sgst": 3660.00,
            "tds_amount": 6100.00, "net_payable_claimed": 62220.00,
        },
    },
    "Advice matching vendor's outdated rate (INV-17)": {
        "invoice_id": 17,
        "submitted_advice": {
            "base_amount": 110000.00, "category": "Software",
            "gst_rate_pct": 18.0, "gst_cgst": 9900.00, "gst_sgst": 9900.00,
            "tds_amount": 11000.00, "net_payable_claimed": 118800.00,
        },
    },
    "Correct numbers, but vendor is blocked (INV-24)": {
        "invoice_id": 24,
        "submitted_advice": {
            "base_amount": 30000.00, "category": "Services",
            "gst_rate_pct": 18.0, "gst_cgst": 2700.00, "gst_sgst": 2700.00,
            "tds_amount": 3000.00, "net_payable_claimed": 32400.00,
        },
    },
    # No invoice_id at all -- this is the OTHER real UC2 scenario: a draft
    # advice for something not yet entered anywhere. Deliberately uses a
    # stale/incorrect rate (12% instead of the actual current 18% for
    # Furniture) so this example demonstrates the rate-check catching a
    # real error, not just a clean pass.
    "Draft advice for a NEW invoice (not yet recorded)": {
        "submitted_advice": {
            "base_amount": 15000.00, "category": "Furniture",
            "gst_rate_pct": 12.0, "gst_cgst": 1000.00, "gst_sgst": 1000.00,
            "tds_amount": 0.00, "net_payable_claimed": 17000.00,
        },
    },
}

def _validate_and_render(payload: dict, key_suffix: str) -> None:
    """Validates one payment advice and renders its full result (verdict,
    field-by-field table, eligibility warning, evidence expanders). Used
    both for a single submission and in a loop for a multi-row CSV/upload
    batch -- key_suffix must be unique per call in the same script run
    (e.g. the row index) since the three verdict containers below use an
    explicit key= for CSS styling, and Streamlit raises a duplicate-key
    error if the same key renders twice in one run. The two st.expander()
    calls further down deliberately do NOT take a key= -- this Streamlit
    version's st.expander() doesn't accept one at all (confirmed against
    the installed version directly; an earlier attempt to add one raised
    "unexpected keyword argument 'key'" the moment a second row rendered),
    and it turns out not to be needed: two expanders with the identical
    label in the same run were confirmed live to render with no collision,
    since Streamlit disambiguates their internal IDs by call order on its
    own when no key is given."""
    if "submitted_advice" not in payload:
        st.error("Missing required field: 'submitted_advice'.")
        return

    with st.spinner("Independently reconstructing the correct figure, then comparing..."):
        try:
            resp = requests.post(f"{N8N_BASE}/webhook/uc2-validate", json=payload, timeout=90)
            result = resp.json()
        except Exception as e:
            result = {"error": "request_failed", "message": str(e)}

    if result.get("error") == "not_found":
        st.error(result["message"])
        try:
            sample = _load_invoice_options()[:6]
        except Exception:
            sample = []
        if sample:
            st.caption("Existing invoices you can validate against: " +
                       ", ".join(f"INV-{r['invoice_id']} ({r['vendor']})" for r in sample))
        st.caption("Validating a new entry not in the system yet? Use Manual Form → "
                   "\"new entry (no PO or invoice recorded yet)\", or omit invoice_id from JSON/CSV.")
    elif result.get("error"):
        st.error(f"Something went wrong: {result.get('message', result)}")
    else:
        diff = result.get("diff", {})
        is_category_only = result.get("mode") == "category_only"

        if is_category_only:
            st.info("📝 " + result.get("note", "This invoice is not yet recorded in the system — "
                    "reduced check: only the tax rate was verified."))

        if diff.get("blocked"):
            # Same underlying condition as UC1's tax_treatment_refused
            # (a category conflict with no defensible rule) -- reuses
            # the exact same calm info-blue .refusal-block so the two
            # surfaces read identically instead of this one alone
            # using an amber warning.
            st.markdown(
                f'<div class="refusal-block"><div class="heading">⏸ Held for review</div>'
                f'{html.escape(diff.get("blocked_reason", ""))}</div>',
                unsafe_allow_html=True,
            )
        elif diff.get("overall_match"):
            # st.markdown() on the RAW text (no html.escape, no
            # wrapping <div>) -- verdict is LLM-generated narration
            # that can itself contain markdown (a heading, **bold**,
            # lists). Confirmed live: wrapping it in a hand-built
            # <div unsafe_allow_html> broke block-level markdown
            # specifically -- a "# Heading" line rendered as a
            # literal "# Heading" instead of an actual heading,
            # because once content sits inside an explicit HTML
            # block, Streamlit's markdown parser still does INLINE
            # parsing (which is why **bold** happened to still work)
            # but stops doing BLOCK-level parsing. st.container(key=)
            # gives a real, plain (borderless) DOM element to color
            # via CSS instead, with the text going through
            # Streamlit's normal, full markdown path.
            with st.container(key=f"verdict_ok_{key_suffix}"):
                st.markdown("✓ " + result.get("verdict", "Advice matches the independently computed result."))
        else:
            with st.container(key=f"verdict_bad_heading_{key_suffix}"):
                st.markdown("✗ Divergence found")
            with st.container(key=f"verdict_bad_text_{key_suffix}"):
                st.markdown(result.get("verdict", ""))

        # Deliberately separate from the numeric verdict above: whether
        # the numbers are correct is a different question from whether
        # this payment is actually clear to release. st.warning (not
        # st.error) is used specifically so this gets its own color
        # family via the CSS above -- a numeric mismatch and an
        # eligibility hold must never be visually confusable, even
        # when (as here) the numbers are otherwise completely correct.
        # Never shown for category_only -- eligibility genuinely
        # cannot be assessed without a real vendor/PO/invoice record,
        # so it's omitted entirely rather than defaulting to a claim
        # ("eligible: true") that was never actually verified.
        if not is_category_only and not diff.get("eligible", True):
            st.divider()
            st.warning("🚫 **NOT CLEAR TO PAY** — regardless of whether the numbers above match:\n\n"
                      + "\n".join(f"- {r}" for r in diff.get("eligibility_reasons", [])))

        # Same advisory UC1 already shows for this invoice -- an
        # advance never gets netted into overall_match/eligible, so
        # a numerically "correct" advice must never read as
        # unconditionally clear to release in full when one exists.
        # Never present for category_only (no real advance data
        # exists for an invoice that isn't recorded yet).
        if not is_category_only and diff.get("unapplied_advance_advisory"):
            st.info(f"⚠️ Advisory: an unapplied advance of ₹{diff['unapplied_advance_advisory']:,.0f} "
                    f"exists against this vendor/PO and has NOT been netted here — a possible overpayment risk if missed.")

        # Non-PO invoice with no linked office -- the true CGST/SGST-vs-IGST
        # split couldn't be independently determined (see diff.py). Total
        # GST/net-payable are still fully verified below; this only flags
        # that the split classification itself wasn't checked.
        if diff.get("split_undetermined"):
            st.caption("ℹ️ " + (diff.get("split_undetermined_note") or
                       "This invoice has no linked purchase order/office on file, so the CGST/SGST-vs-IGST "
                       "split couldn't be independently verified — only the total GST amount was checked."))

        fields = diff.get("fields", [])
        if is_category_only:
            # Collapse cgst+sgst into one "GST amount" row -- the
            # split itself was never checked (see the note above,
            # and the Manual Form's "GST amount" help text) -- and
            # drop base_amount entirely: it was never independently
            # verified, just the accountant's own figure fed back
            # to itself, so showing it as a "✅ match" row would be
            # misleading rather than informative.
            cgst_f = next((f for f in fields if f["field"] == "gst_cgst"), None)
            sgst_f = next((f for f in fields if f["field"] == "gst_sgst"), None)
            fields = [f for f in fields if f["field"] not in ("base_amount", "gst_cgst", "gst_sgst")]
            if cgst_f and sgst_f:
                both_match = cgst_f["match"] and sgst_f["match"]
                fields.insert(0, {
                    "field": "gst_amount",
                    "claimed": (cgst_f["claimed"] or 0) + (sgst_f["claimed"] or 0),
                    "correct": (cgst_f["correct"] or 0) + (sgst_f["correct"] or 0),
                    "match": both_match,
                    "reason": "" if both_match else "GST amount does not match the applicable rate for this category and date.",
                })

        if fields:
            st.write("**Field-by-field comparison:**")
            rows = []
            for f in fields:
                rows.append({
                    "Field": f["field"],
                    "Submitted": f["claimed"],
                    "Correct": f["correct"],
                    "Match?": "✓" if f["match"] else "✗",
                    "Why (if mismatched)": f["reason"],
                })
            df = pd.DataFrame(rows)

            def _match_style(val):
                # pandas Styler renders these as inline style="..."
                # attributes, which beat any external stylesheet rule
                # on specificity alone -- confirmed live (the CSS
                # dark-theme pass had zero effect here until these
                # literal colors were fixed at the source). var()
                # references still resolve correctly in an inline
                # attribute since :root is an ancestor of the table.
                # Plain "✓"/"✗" (not emoji "✅"/"❌") specifically so
                # the `color` below actually reaches the glyph --
                # emoji are drawn from a color-emoji font table that
                # ignores CSS color in every browser, so the
                # conditional text-coloring here would otherwise be a
                # silent no-op (background-color still worked, which
                # is why this wasn't caught until compared glyph-by-
                # glyph against the mockup's own literal HTML).
                if val == "✓":
                    return "background-color: var(--success-bg); color: var(--success-text); font-weight: 600;"
                if val == "✗":
                    return "background-color: var(--error-bg); color: var(--error-text); font-weight: 600;"
                return ""

            # st.table (not st.dataframe) deliberately -- st.dataframe
            # renders to a <canvas> grid with no per-cell DOM, so CSS
            # and this Styler-based formatting can't reach it at all.
            # st.table renders real HTML, so both work. Same reasoning
            # applies to every property below: set explicitly here,
            # not left to inheritance from the page's dark palette.
            styled = (
                df.style
                .hide(axis="index")
                # Broad defaults FIRST, then .map()'s per-cell
                # background LAST -- pandas Styler concatenates
                # style rules in call order into one inline style="
                # ..." attribute, and CSS's own "last property wins"
                # rule then applies. Confirmed live: with .map()
                # before this broad set_properties(), the flat
                # background-color here was silently clobbering the
                # conditional success/error colors on every match/
                # mismatch cell.
                .set_properties(**{"background-color": "var(--panel)", "color": "var(--text)",
                                    "border-color": "var(--panel-border)"})
                .set_properties(subset=["Submitted", "Correct", "Match?"], **{
                    "font-family": "ui-monospace, SFMono-Regular, monospace",
                    # Center-aligned against a centered header, not
                    # right-aligned against a left-aligned one -- a
                    # real readability mismatch caught while building
                    # this mockup.
                    "text-align": "center",
                })
                .set_table_styles([
                    {"selector": "th", "props": "background-color: var(--card); color: var(--muted); "
                                                  "border-color: var(--panel-border); text-align: center; "
                                                  "font-weight: 500; font-size: 12px;"},
                    {"selector": "th:first-child, td:first-child", "props": "text-align: left;"},
                ])
                .map(_match_style, subset=["Match?"])
            )
            st.table(styled)

        if result.get("tax_evidence"):
            with st.expander("Tax document sources for this reconstruction"):
                for label, key in [("GST", "gst"), ("TDS", "tds")]:
                    sub = result["tax_evidence"].get(key)
                    if sub and sub.get("status") == "found":
                        st.markdown(f"**{label}** — *{sub['source_filename']}*")
                        st.markdown(f"> {sub['key_clause']}")

        with st.expander("Full raw response"):
            _render_json_block(result)


with tab_validate:
    st.markdown(
        '<div class="section-title">Validate a Payment Advice</div>'
        '<div class="section-sub">An accountant\'s payment advice is uploaded, pasted, or entered — the '
        'agent independently reconstructs the correct figure first, <em>then</em> compares.</div>',
        unsafe_allow_html=True,
    )

    mode = st.radio("Input method", ["Use an example", "Manual form", "Paste JSON or CSV row", "Upload a file"],
                     horizontal=True, label_visibility="collapsed")

    payloads = None

    # Each mode's ENTIRE UI lives in its own explicitly-keyed container --
    # confirmed live (with real user clicks, not just reasoning about it)
    # that switching modes could leave the PREVIOUS mode's widgets fully
    # rendered and visible for a few seconds, in both directions, most
    # visibly with st.form() specifically (e.g. "Use an example" active,
    # but the whole Manual Form -- radios, invoice picker, all 6 number
    # fields -- still on screen underneath it). Direct timing tests ruled
    # out a slow blocking call as the cause here (a plain mode-switch
    # rerun completes in well under a second once triggered) -- this reads
    # as a front-end reconciliation quirk specific to diffing very
    # differently-shaped widget trees (2 widgets vs. ~15, including a
    # <form>) across a conditional branch with no explicit key of its own
    # to swap on. Giving each branch its own container key lets Streamlit
    # identify "this whole subtree is gone" unambiguously instead of
    # diffing individual mismatched positions one at a time.
    if mode == "Use an example":
        with st.container(key="mode_use_example"):
            choice = st.selectbox("Pick a scenario", list(FIXTURES.keys()), index=None,
                                   placeholder="Choose a scenario to validate...")
            if choice is None:
                st.caption("Five scenarios to try: a clean match, multiple errors, a vendor's own outdated "
                           "rate, a blocked vendor, and a draft advice for an invoice not yet in the system.")
            else:
                _render_json_block(FIXTURES[choice])
                if st.button("Validate this advice", type="primary"):
                    payloads = [FIXTURES[choice]]

    elif mode == "Manual form":
        with st.container(key="mode_manual_form"):
            # "Is this recorded?" used to be its own separate Yes/No radio,
            # entirely redundant with picking a specific invoice below --
            # picking a real invoice already means "existing", so the only
            # genuinely distinct choice is "there is no real invoice at
            # all", now folded into the SAME dropdown as one more option.
            NEW_ENTRY_LABEL = "+ New entry (no PO or invoice recorded yet)"
            try:
                # DATABASE_URL points at a remote Postgres instance (even in
                # local dev), so an uncached call here is a real network
                # round-trip, not an in-process lookup -- confirmed live
                # that switching into this mode right after an "Use an
                # example" result, with nothing shown while this blocks,
                # left the PREVIOUS result sitting on screen for a few
                # seconds with no indication why, since Streamlit doesn't
                # clear that later part of the script's own output until
                # execution actually reaches it again. @st.cache_data makes
                # every call after the first near-instant, but the first
                # one (or the first after the 60s TTL expires) still needs
                # to be visibly accounted for.
                with st.spinner("Loading invoice list..."):
                    invoices = _load_invoice_options()
            except Exception as e:
                # Same discipline as Browse Mock Data: never echo a raw DB
                # exception (it can contain a misconfigured secret) into a
                # public-facing message.
                print(f"[uc2-validate-picker] Could not load invoice list: {e}")
                invoices = []
                st.warning("Could not load the list of existing invoices right now — try Paste/Upload "
                           "instead, or choose \"" + NEW_ENTRY_LABEL + "\" below.")

            # is_igst per invoice: None means "can't tell" (no linked PO ->
            # no office to compare the vendor's state against) rather than
            # silently defaulting to one or the other -- confirmed against
            # the real seed data that this genuinely happens (1 of 25
            # invoices has no PO), not a hypothetical edge case.
            inv_meta = {
                f"INV-{r['invoice_id']} · {r['vendor']} · {r['category']} · "
                f"₹{r['base_amount']:,.0f} · {r['status']}": {
                    "invoice_id": r["invoice_id"],
                    "is_igst": (r["registered_state"] != r["office_state"]) if r["office_state"] else None,
                }
                for r in invoices
            }
            inv_options = {label: meta["invoice_id"] for label, meta in inv_meta.items()}

            # Deliberately OUTSIDE the form below: st.form only reruns the
            # script on submit, not on interior widget changes, so this
            # couldn't immediately swap which fields are visible before the
            # user submits -- confirmed live with the GST-split radio that
            # used to live here hitting this exact issue (selecting IGST
            # left the CGST/SGST boxes on screen; the real IGST box only
            # appeared on the next run, after submission had already
            # captured the old branch's values). Reading it here instead
            # makes the field swap immediate.
            picked_label = st.selectbox(
                "Which invoice?", [NEW_ENTRY_LABEL] + list(inv_options.keys()), index=None,
                placeholder="Search or select an invoice, or choose a new entry...",
            )
            is_existing = picked_label is not None and picked_label != NEW_ENTRY_LABEL

            is_igst = None
            if is_existing:
                auto_is_igst = inv_meta[picked_label]["is_igst"]
                if auto_is_igst is None:
                    st.caption("This invoice has no linked purchase order, so the CGST/SGST vs. IGST split "
                               "can't be auto-detected from vendor/office state — please select it directly.")
                    split = st.radio("GST split", ["CGST + SGST (intra-state)", "IGST (inter-state)"],
                                      label_visibility="collapsed")
                    is_igst = split.startswith("IGST")
                else:
                    is_igst = auto_is_igst
                    st.caption(f"Auto-detected: {'IGST (inter-state)' if is_igst else 'CGST + SGST (intra-state)'}, "
                               f"based on this vendor and office's registered states.")

            with st.form("manual_advice_form", border=False):
                invoice_date = None
                if not is_existing:
                    st.caption("⚠️ **Reduced check** — this invoice has no record yet, so only the GST/TDS "
                               "**rate** math can be verified against current category tax rules. Base amount "
                               "is taken as given, and 3-way match / vendor eligibility can't be checked.")
                    invoice_date = st.date_input("Invoice date (optional — defaults to today)", value=None)

                col_a, col_b = st.columns(2)
                with col_a:
                    category = st.selectbox("Category", CATEGORY_OPTIONS, index=None,
                                             placeholder="Select a category...")
                with col_b:
                    base_amount = st.number_input("Base amount (₹)", min_value=0.0, step=100.0,
                                                   value=None, placeholder="0.00")

                # col_c/col_d kept as one pair even when col_d ends up empty (the
                # CGST+SGST branch below) -- real field counts differ from the
                # mockup's clean 4-pair layout across the 3 GST-split modes, and
                # keeping this slot reserved is what lets CGST+SGST form their
                # own dedicated pair further down while the grid rhythm stays
                # visually consistent across all three modes.
                col_c, col_d = st.columns(2)
                with col_c:
                    gst_rate_pct = st.number_input(
                        "GST rate (%)", min_value=0.0, step=0.5, value=None, placeholder="e.g. 18",
                        help="Enter as a whole percentage — e.g. 18 for 18%, not 0.18.",
                    )

                gst_cgst = gst_sgst = gst_igst = gst_total = None
                if is_existing:
                    if is_igst:
                        with col_d:
                            gst_igst = st.number_input("IGST (₹)", min_value=0.0, step=10.0,
                                                        value=None, placeholder="0.00")
                    else:
                        col_e, col_f = st.columns(2)
                        with col_e:
                            gst_cgst = st.number_input("CGST (₹)", min_value=0.0, step=10.0,
                                                        value=None, placeholder="0.00")
                        with col_f:
                            gst_sgst = st.number_input("SGST (₹)", min_value=0.0, step=10.0,
                                                        value=None, placeholder="0.00")
                else:
                    with col_d:
                        gst_total = st.number_input(
                            "GST amount (₹)", min_value=0.0, step=10.0, value=None, placeholder="0.00",
                            help="Combined CGST+SGST or IGST, whichever you have — only the RATE this implies "
                                 "is checked, not which split applies (that needs a real vendor/office pairing, "
                                 "which doesn't exist yet for an unrecorded invoice).",
                        )

                col_g, col_h = st.columns(2)
                with col_g:
                    tds_amount = st.number_input("TDS amount (₹)", min_value=0.0, step=10.0,
                                                  value=None, placeholder="0.00")
                with col_h:
                    net_payable_claimed = st.number_input("Net payable claimed (₹)", min_value=0.0, step=10.0,
                                                           value=None, placeholder="0.00")
                submitted = st.form_submit_button("Validate this advice", type="primary")

            if submitted:
                missing = []
                if picked_label is None:
                    missing.append('invoice (or choose "' + NEW_ENTRY_LABEL + '")')
                if category is None:
                    missing.append("category")
                if base_amount is None:
                    missing.append("base amount")
                if gst_rate_pct is None:
                    missing.append("GST rate")
                if is_existing and gst_cgst is None and gst_igst is None:
                    missing.append("GST amount (CGST+SGST or IGST)")
                if not is_existing and gst_total is None:
                    missing.append("GST amount")
                if tds_amount is None:
                    missing.append("TDS amount")
                if net_payable_claimed is None:
                    missing.append("net payable claimed")

                if missing:
                    st.error("Missing required field(s): " + ", ".join(missing) + ".")
                else:
                    advice = {"base_amount": base_amount, "category": category, "gst_rate_pct": gst_rate_pct,
                              "tds_amount": tds_amount, "net_payable_claimed": net_payable_claimed}
                    if is_existing:
                        if gst_igst is not None:
                            advice["gst_igst"] = gst_igst
                        else:
                            advice["gst_cgst"] = gst_cgst
                            advice["gst_sgst"] = gst_sgst
                        payloads = [{"invoice_id": inv_options[picked_label], "submitted_advice": advice}]
                    else:
                        # No split choice was offered (see the help text above) --
                        # divide evenly, well within the ±₹1 diff tolerance of
                        # compute.py's own even CGST/SGST split, so the RATE
                        # check this mode actually promises stays accurate.
                        advice["gst_cgst"] = gst_total / 2
                        advice["gst_sgst"] = gst_total / 2
                        payload = {"submitted_advice": advice}
                        if invoice_date:
                            payload["invoice_date"] = invoice_date.isoformat()
                        payloads = [payload]

    elif mode == "Paste JSON or CSV row":
        with st.container(key="mode_paste"):
            example_csv = "invoice_id,base_amount,category,gst_rate_pct,gst_cgst,gst_sgst,tds_amount,net_payable_claimed\n17,110000,Software,18,9900,9900,11000,118800"
            pasted = st.text_area(
                "Paste JSON matching {invoice_id, submitted_advice: {...}}, or a CSV header + one or more "
                f"data rows (up to {MAX_CSV_ROWS}, e.g. copied straight out of a spreadsheet). Omit invoice_id "
                "entirely for a draft advice on an invoice that isn't recorded yet — only the tax rate will be "
                "checked in that case.",
                height=200,
                placeholder=f"{json.dumps(FIXTURES['Correct advice (INV-1)'], indent=2)}\n\n--- or ---\n\n{example_csv}",
            )
            if st.button("Validate this advice", type="primary"):
                stripped = pasted.strip()
                try:
                    # Auto-detect rather than making the user pick a sub-mode --
                    # JSON always starts with '{'; anything else is treated as a
                    # CSV header + one or more data rows (the shape a spreadsheet
                    # paste takes). A single JSON object is still exactly one
                    # payload -- multi-row batching is a CSV-only capability for
                    # now, matching what was actually asked for.
                    if stripped.startswith("{"):
                        payloads = [json.loads(stripped)]
                    else:
                        payloads = _parse_csv_rows(stripped)
                except json.JSONDecodeError as e:
                    st.error(f"That's not valid JSON: {e}")
                except (ValueError, StopIteration) as e:
                    st.error(f"Couldn't parse that as JSON or CSV: {e}")

    elif mode == "Upload a file":
        with st.container(key="mode_upload"):
            uploaded = st.file_uploader("Upload a .json or .csv file", type=["json", "csv"])
            st.caption("An invoice_id column/key is only required if this invoice already has a record — "
                       f"omit it for a draft advice on something not yet entered anywhere. A CSV may have up "
                       f"to {MAX_CSV_ROWS} data rows, each validated in turn.")
            if uploaded and st.button("Validate this advice", type="primary"):
                try:
                    if uploaded.name.lower().endswith(".csv"):
                        payloads = _parse_csv_rows(uploaded.getvalue().decode("utf-8"))
                    else:
                        payloads = [json.load(uploaded)]
                except json.JSONDecodeError as e:
                    st.error(f"That's not valid JSON: {e}")
                except (ValueError, StopIteration) as e:
                    st.error(f"Couldn't parse that CSV: {e}")

    if payloads:
        for i, payload in enumerate(payloads):
            if len(payloads) > 1:
                label = f"Invoice #{payload['invoice_id']}" if payload.get("invoice_id") else "Draft (not yet recorded)"
                st.markdown(f"#### Row {i + 1} of {len(payloads)} — {label}")
            _validate_and_render(payload, key_suffix=str(i))
            if i < len(payloads) - 1:
                st.divider()
# ---------------------------------------------------------------------------
# TAB 3 — Browse the mock data
# ---------------------------------------------------------------------------

def _section_header(title: str, source_label: str):
    """Labels each table with which of the three sources it belongs to --
    per the domain model, Vendors/Invoices are Source B (SAP-style Vendor &
    Payments DB); Offices/Requisitions/Purchase Orders/Goods Receipts are
    Source A (Procurement Portal). The reference mockup had these two
    swapped -- corrected here to match the actual data model, not copied
    from the mockup verbatim."""
    st.markdown(f'<div style="display:flex; align-items:baseline; gap:10px; margin:8px 0 14px;">'
                f'<h3 style="margin:0; font-size:14px; font-weight:500; color:var(--muted);">{title}</h3>'
                f'<span style="font-size:11px; color:var(--accent); border:1px solid '
                f'rgba(91, 143, 217, 0.45); border-radius:4px; '
                f'padding:2px 7px;">{source_label}</span></div>', unsafe_allow_html=True)


with tab_browse:
    st.markdown(
        '<div class="section-title">Browse Mock Data</div>'
        '<div class="section-sub">Read-only view of the vendors, invoices, purchase orders, and tax '
        'documents in the system — useful context before asking a question or validating a payment '
        'advice.</div>',
        unsafe_allow_html=True,
    )
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        _section_header("Vendors", "Source B · Postgres")
        cur.execute("""
            SELECT v.vendor_id, v.legal_name, v.registered_state, v.gstin, v.payment_terms, v.status
            FROM vendor_master v ORDER BY v.vendor_id;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True, height=260)

        _section_header("Invoices", "Source B · Postgres")
        cur.execute("""
            SELECT i.invoice_id, v.legal_name AS vendor, c.category_name AS category,
                   i.invoice_date, i.base_amount, i.status
            FROM invoice i
            JOIN vendor_master v ON i.vendor_id = v.vendor_id
            JOIN category c ON i.category_id = c.category_id
            ORDER BY i.invoice_date DESC;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True, height=260)

        _section_header("Offices", "Source A · Postgres")
        cur.execute("SELECT office_id, name, city, state FROM office ORDER BY office_id;")
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True, height=260)

        # Source A's own "structured workflow from request to fulfillment" --
        # previously queried nowhere in the frontend, even though it's the
        # direct input to every three-way-match and cancelled-PO/category-
        # conflict eligibility computation this system performs. Found by
        # recruiter-mindset testing: that reasoning is real and correct, but
        # was undiscoverable to anyone restricted to the live app. Read-only,
        # same pattern as Vendors/Invoices/Offices above -- no new tables,
        # nothing built, only surfaced.
        _section_header("Requisitions", "Source A · Postgres")
        cur.execute("""
            SELECT r.requisition_id, o.name AS office, r.requester, r.department,
                   c.category_name AS category, r.estimated_amount, r.status, r.created_date
            FROM requisition r
            JOIN office o ON r.office_id = o.office_id
            JOIN category c ON r.category_id = c.category_id
            ORDER BY r.requisition_id;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True, height=260)

        _section_header("Purchase Orders", "Source A · Postgres")
        cur.execute("""
            SELECT po.po_id, v.legal_name AS vendor, c.category_name AS category,
                   po.po_amount, po.issued_date, po.status
            FROM purchase_order po
            JOIN vendor_master v ON po.vendor_id = v.vendor_id
            JOIN category c ON po.category_id = c.category_id
            ORDER BY po.po_id;
        """)
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True, height=260)

        _section_header("Goods Receipts", "Source A · Postgres")
        cur.execute("SELECT receipt_id, po_id, received_date, received_amount, status FROM receipt ORDER BY receipt_id;")
        st.dataframe([dict(r) for r in cur.fetchall()], use_container_width=True, hide_index=True, height=260)

        conn.close()
    except Exception as e:
        # Never interpolate the raw exception into a user-facing message --
        # a driver-level connection error (e.g. "invalid dsn") echoes back
        # the malformed connection string itself, which can contain a
        # misconfigured secret. Log server-side (Render's own log tab, not
        # public) for actual debugging; show only a generic message here.
        print(f"[browse-mock-data] DB connection failed: {e}")
        st.error("Could not reach the database. This is a configuration issue on the "
                 "backend, not something on your end -- the mock-data browser is "
                 "temporarily unavailable.")

    # Source C -- read directly from the repo checkout (not Postgres), so this
    # section still renders even if the DB connection above fails.
    _section_header("Tax & Regulatory Documents", "Source C · Unstructured files")
    st.caption("All documents below are **synthetic** -- written for this assignment to "
               "read like real Indian GST/TDS circulars, not real government issuances. "
               "See tax_docs/README.md in the repo for the full rationale behind each one.")
    tax_docs_dir = os.path.join(os.path.dirname(__file__), "..", "tax_docs")

    def _doc_category_tag(fname: str) -> str:
        if fname.startswith("gst_"):
            return "GST"
        if fname.startswith("tds_"):
            return "TDS"
        if fname.startswith("state_"):
            return "STATE"
        return "DOC"

    try:
        doc_files = sorted(f for f in os.listdir(tax_docs_dir)
                            if f.endswith(".md") and f != "README.md")
        for fname in doc_files:
            with open(os.path.join(tax_docs_dir, fname), encoding="utf-8") as f:
                body = f.read()
            title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else fname
            tag = _doc_category_tag(fname)
            with st.expander(f"[{tag}]  {title}  ·  `{fname}`"):
                st.markdown(body)
    except FileNotFoundError:
        st.warning("Tax document corpus not found alongside this deployment.")
