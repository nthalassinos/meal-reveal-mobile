import os
import hmac
import hashlib
import base64
import re
from numbers import Real
from pathlib import Path
from uuid import uuid4
from diary import (CORE_NUTRIENT_WARNINGS, Diary, DiaryError, combine_food_records,
                   expand_recipe_components, scale_recipe_components, missing_core_nutrients,
                   ordered_food_frame, summarize)
from eurofir import (CATEGORY_OPTIONS, category_index, estimate_final_weight,
                     infer_category, looks_already_cooked, method_group,
                     COMPONENT_YIELD_MODE, COMPONENT_YIELD_WARNING,
                     ingredient_yield_profiles_for,
                     yield_profile_documentation, yield_profiles_for)
from diary_ui import login_diary, history, meal_progress, save_meal
from profile_ui import require_profile, profile_form
from backup_ui import backup_dashboard
from period_ui import period_dashboard
from nutrient_ui import compact_nutrient_summary, nutrient_dashboard
from supplement_ui import supplement_dashboard
from product_capture_ui import product_capture_dashboard
from recipe_ui import recipe_dashboard
from weight_ui import weight_dashboard
from dsld import dsld_label, dsld_product_by_barcode, dsld_search
from off_supplements import (merge_external_supplement_results,
                             open_food_facts_supplement_search)
from medas_ui import medas_dashboard
from upf_ui import upf_dashboard
import pandas as pd
import streamlit as st
from streamlit_searchbox import st_searchbox
from barcode_scanner import live_barcode_scanner
from core import (recognize, search_food, food_detail, portion_nutrients, portion_from_per100,
                  open_food_facts_product, search_personal_foods, search_shared_barcode_foods,
                  apply_best_food_matches, barcode_from_option, barcode_history_options,
                  NUTRIENTS, ServiceError)
from upf import (NOVA_OPTIONS, NOVA_CHOICE_HELP, normalize_nova,
                 nova_from_option, nova_short_label)
from medas import (MEDAS_CATEGORY_OPTIONS, MEDAS_CHOICE_HELP, OUTSIDE_CATEGORY_LABEL,
                   AMBIGUOUS_COMPOSITE_LABEL, AMBIGUOUS_MEDAS_STATUS,
                   ambiguous_medas_categories, classify_food, is_known_non_target,
                   medas_categories_from_options, medas_category_text,
                   medas_classification_source, medas_component_allocation,
                   medas_component_label, medas_options_from_categories,
                   normalize_medas_categories, normalize_medas_component_amounts)
from download_ui import download_menu
from notification_ui import (load_notifications, notification_banner,
                             notifications_dashboard, unread_count)
from greek_cheeses import search_greek_cheeses

APP_DIR = Path(__file__).resolve().parent
LOGO_PATH = APP_DIR / "assets" / "meal-reveal-logo.png"
FAVICON_PATH = APP_DIR / "assets" / "meal-reveal-favicon.png"
APP_VERSION = "2026.10.26-product-capture-menu-hotfix-1"
PERSONAL_FOODS_CACHE_VERSION = "medas-composite-allocation-products-v1"

NOVA_METADATA_KEYS = (
    "nova_status", "upf_markers", "additive_functions", "nova_reason",
    "classification_method", "classification_version", "source_reference",
    "ingredient_list", "processing_description", "ingredients_complete",
    "industrial_process", "brand", "barcode", "additives",
)

st.set_page_config(
    page_title="Meal Reveal · Ανάλυση γεύματος",
    page_icon=str(FAVICON_PATH) if FAVICON_PATH.exists() else (
        str(LOGO_PATH) if LOGO_PATH.exists() else "🍽️"
    ),
    layout="centered",
    initial_sidebar_state="collapsed",
)


def themed_dataframe(frame, **kwargs):
    """Render responsive tables with stable colours and readable precision."""
    dark = (
        st.session_state.get("display_theme_control") == "Σκοτεινό"
        or st.query_params.get("display_theme") == "dark"
    )
    background = "#252936" if dark else "#ffffff"
    alternate = "#2d3240" if dark else "#f7f8fa"
    header = "#313746" if dark else "#eef1f5"
    text = "#fafafa" if dark else "#262730"
    border = "#555b66" if dark else "#d7dce2"

    def display_value(value):
        try:
            missing = pd.isna(value)
            if not hasattr(missing, "__iter__") and bool(missing):
                return "—"
        except (TypeError, ValueError, AttributeError):
            pass
        if isinstance(value, Real) and not isinstance(value, bool):
            number = float(value)
            absolute = abs(number)
            if abs(number - round(number)) < 1e-10:
                return f"{number:,.0f}"
            decimals = (1 if absolute >= 10 else 2 if absolute >= 0.1
                        else 3 if absolute >= 0.01 else 4 if absolute >= 0.001
                        else 5)
            return f"{number:,.{decimals}f}".rstrip("0").rstrip(".")
        return value

    shown = frame.copy()
    for column in shown.columns:
        shown[column] = shown[column].map(display_value)
    show_index = not kwargs.get("hide_index", False)
    table_html = shown.to_html(index=show_index, escape=True, border=0, na_rep="—")
    st.html(
        f"""
        <style>
        .mr-table-wrap {{ width:100%; overflow-x:auto; margin:.35rem 0 1rem; }}
        .mr-table-wrap table {{ width:100%; border-collapse:collapse; background:{background};
          color:{text}; font-size:.88rem; font-variant-numeric:tabular-nums; }}
        .mr-table-wrap th {{ background:{header}; color:{text}; font-weight:600; }}
        .mr-table-wrap th, .mr-table-wrap td {{ border:1px solid {border};
          padding:.5rem .6rem; text-align:left; white-space:nowrap; }}
        .mr-table-wrap tbody tr:nth-child(even) td {{ background:{alternate}; }}
        </style>
        <div class="mr-table-wrap">{table_html}</div>
        """
    )

# Streamlit stacks columns on narrow screens. Keep only the quantity/unit pair
# side by side; the marker below scopes the rule so no other app layout changes.
st.markdown(
    """
    <style>
    @media (max-width: 640px) {
        div[data-testid="stHorizontalBlock"]:has(.mr-quantity-row-marker):not(:has(div[data-testid="stHorizontalBlock"])) {
            flex-wrap: nowrap !important;
            gap: 0.5rem !important;
            align-items: flex-end !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.mr-quantity-row-marker):not(:has(div[data-testid="stHorizontalBlock"]))
        > div[data-testid="stColumn"]:first-child {
            flex: 1 1 72% !important;
            width: 72% !important;
            min-width: 0 !important;
        }
        div[data-testid="stHorizontalBlock"]:has(.mr-quantity-row-marker):not(:has(div[data-testid="stHorizontalBlock"]))
        > div[data-testid="stColumn"]:last-child {
            flex: 0 0 28% !important;
            width: 28% !important;
            min-width: 5.2rem !important;
        }
        .mr-quantity-row-marker { display: none; }
    }
    .mr-medas-composite-marker { display: none; }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.mr-medas-composite-marker) {
        border-color: #d64545 !important;
        border-width: 2px !important;
        background: rgba(214, 69, 69, 0.06) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def render_brand_header():
    """Show the app logo at a responsive, unobtrusive size on every page."""
    if not LOGO_PATH.exists():
        return
    encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    st.markdown(
        f"""
        <style>
        .meal-reveal-brand {{
            display: flex;
            justify-content: center;
            align-items: center;
            margin: -1rem 0 0.25rem;
        }}
        .meal-reveal-brand img {{
            display: block;
            width: min(190px, 42vw);
            height: auto;
        }}
        @media (max-width: 640px) {{
            .meal-reveal-brand {{
                margin-top: -0.5rem;
                margin-bottom: 0.1rem;
            }}
            .meal-reveal-brand img {{
                width: min(145px, 40vw);
            }}
        }}
        </style>
        <div class="meal-reveal-brand">
            <img src="data:image/png;base64,{encoded}" alt="Meal Reveal">
        </div>
        """,
        unsafe_allow_html=True,
    )


BROWSER_BEHAVIORS_JS = r"""
export default function(component) {
  const { data } = component;
  const doc = document;

  if (window.__mealRevealBrowserCleanup) {
    window.__mealRevealBrowserCleanup();
  }

  const collapseButton = () => {
    const holder = doc.querySelector('[data-testid="stSidebarCollapseButton"]');
    if (!holder) return null;
    return holder.querySelector('button') || holder;
  };
  const closeSidebar = () => {
    const button = collapseButton();
    if (button && button.getClientRects().length) button.click();
  };
  const closeDialog = (dialog) => {
    const button =
      dialog.querySelector('button[aria-label="Close"]') ||
      dialog.querySelector('button[aria-label="Κλείσιμο"]') ||
      dialog.querySelector('button[kind="header"]');
    if (button) button.click();
  };
  const hideChartTooltips = () => {
    doc.querySelectorAll('#vg-tooltip-element, .vg-tooltip').forEach((tooltip) => {
      tooltip.classList.remove('visible');
      tooltip.setAttribute('aria-hidden', 'true');
    });
  };

  let gesture = null;
  let startX = 0;
  let startY = 0;
  let tooltipTimer = null;
  let resizeTimer = null;

  const touchStart = (event) => {
    if (event.touches.length !== 1) return;
    const target = event.target;
    const sidebar = target?.closest?.('[data-testid="stSidebar"]');
    const dialog = target?.closest?.('[data-testid="stDialog"]');
    gesture = sidebar ? { type: 'sidebar', element: sidebar }
      : dialog ? { type: 'dialog', element: dialog } : null;
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
  };
  const touchEnd = (event) => {
    if (!gesture || !event.changedTouches.length) return;
    const current = gesture;
    gesture = null;
    const dx = event.changedTouches[0].clientX - startX;
    const dy = event.changedTouches[0].clientY - startY;
    if (Math.abs(dx) <= Math.abs(dy) * 1.15) return;
    if (current.type === 'sidebar' && dx < -45) closeSidebar();
    if (current.type === 'dialog' && dx > 45) closeDialog(current.element);
  };
  const click = (event) => {
    if (event.target?.closest?.('[data-testid="stSidebarNav"] a')) {
      window.setTimeout(closeSidebar, 80);
    }
  };
  const pointerDown = (event) => {
    window.clearTimeout(tooltipTimer);
    if (event.target?.closest?.('[data-testid="stVegaLiteChart"]')) {
      tooltipTimer = window.setTimeout(hideChartTooltips, 3200);
    } else {
      hideChartTooltips();
    }
  };
  const viewportChanged = () => {
    closeSidebar();
    hideChartTooltips();
    window.setTimeout(closeSidebar, 120);
    window.setTimeout(closeSidebar, 420);
  };
  const isMobileLandscape = () => {
    const width = Math.max(window.innerWidth || 0, doc.documentElement.clientWidth || 0);
    const height = Math.max(window.innerHeight || 0, doc.documentElement.clientHeight || 0);
    const coarsePointer = window.matchMedia?.('(pointer: coarse)').matches;
    return width > height && (coarsePointer || height <= 700);
  };
  const resized = () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      hideChartTooltips();
      if (isMobileLandscape()) viewportChanged();
    }, 60);
  };
  const wrapperViewportChanged = (event) => {
    const message = event.data;
    if (!message || message.type !== 'meal-reveal-viewport') return;
    if (!Number.isFinite(message.width) || !Number.isFinite(message.height)) return;
    resized();
  };
  doc.addEventListener('touchstart', touchStart, { passive: true });
  doc.addEventListener('touchend', touchEnd, { passive: true });
  doc.addEventListener('click', click, true);
  doc.addEventListener('pointerdown', pointerDown, true);
  doc.addEventListener('scroll', hideChartTooltips, true);
  window.addEventListener('orientationchange', viewportChanged, { passive: true });
  window.addEventListener('resize', resized, { passive: true });
  window.addEventListener('message', wrapperViewportChanged);
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', resized, { passive: true });
  }

  if (data.closeSidebarOnMount || isMobileLandscape()) {
    window.setTimeout(closeSidebar, 80);
    window.setTimeout(closeSidebar, 300);
  }

  window.__mealRevealBrowserCleanup = () => {
    window.clearTimeout(tooltipTimer);
    window.clearTimeout(resizeTimer);
    doc.removeEventListener('touchstart', touchStart);
    doc.removeEventListener('touchend', touchEnd);
    doc.removeEventListener('click', click, true);
    doc.removeEventListener('pointerdown', pointerDown, true);
    doc.removeEventListener('scroll', hideChartTooltips, true);
    window.removeEventListener('orientationchange', viewportChanged);
    window.removeEventListener('resize', resized);
    window.removeEventListener('message', wrapperViewportChanged);
    if (window.visualViewport) {
      window.visualViewport.removeEventListener('resize', resized);
    }
  };
}
"""

_browser_behaviors_component = st.components.v2.component(
    "meal_reveal_browser_behaviors",
    html='<span class="mr-browser-behaviors" aria-hidden="true"></span>',
    css=":host { display:none !important; height:0 !important; }",
    js=BROWSER_BEHAVIORS_JS,
)


def install_mobile_browser_behaviors(close_sidebar_on_mount=False):
    """Install reliable mobile gestures and dismiss sticky chart tooltips."""
    _browser_behaviors_component(
        data={"closeSidebarOnMount": bool(close_sidebar_on_mount)},
        key="meal_reveal_browser_behaviors_component",
    )


def selected_display_theme():
    label = st.session_state.get("display_theme_control")
    if label in ("Φωτεινό", "Σκοτεινό"):
        return label
    return "Σκοτεινό" if st.query_params.get("display_theme") == "dark" else "Φωτεινό"


def restore_display_theme_from_browser():
    """Restore a saved theme when the mobile wrapper reloads the Streamlit URL."""
    st.iframe(
        """
        <script>
        try {
          const host = window.parent;
          const url = new URL(host.location.href);
          if (!url.searchParams.has("display_theme")) {
            const saved = host.localStorage.getItem("mealRevealDisplayTheme");
            if (saved === "dark" || saved === "light") {
              url.searchParams.set("display_theme", saved);
              host.location.replace(url.toString());
            }
          }
        } catch (_) {}
        </script>
        """,
        height=1, width=1, tab_index=-1,
        alt="Επαναφορά επιλογής εμφάνισης",
    )


def persist_display_theme_in_browser(value):
    st.iframe(
        f"""
        <script>
        try {{
          window.parent.localStorage.setItem("mealRevealDisplayTheme", "{value}");
          if (window.parent.parent && window.parent.parent !== window.parent) {{
            window.parent.parent.postMessage({{
              type: "meal-reveal-theme",
              theme: "{value}"
            }}, "*");
          }}
        }} catch (_) {{}}
        </script>
        """,
        height=1, width=1, tab_index=-1,
        alt="Αποθήκευση επιλογής εμφάνισης",
    )


def apply_display_theme(label):
    dark = label == "Σκοτεινό"
    background = "#0e1117" if dark else "#ffffff"
    sidebar = "#262730" if dark else "#f0f2f6"
    surface = "#1b1f27" if dark else "#ffffff"
    field = "#31333f" if dark else "#ffffff"
    button = "#262730" if dark else "#ffffff"
    hover = "#3a3d49" if dark else "#f0f2f6"
    text = "#fafafa" if dark else "#262730"
    muted = "#c2c7d0" if dark else "#6b7280"
    border = "#555b66" if dark else "#d7dce2"
    help_background = "#f5f7fa" if dark else "#262730"
    help_foreground = "#262730" if dark else "#ffffff"
    st.markdown(
        f"""
        <style>
        html {{ color-scheme: {'dark' if dark else 'light'}; }}
        html, body, #root {{
          width: 100%;
          max-width: 100%;
          min-width: 0;
          overflow-x: hidden;
        }}
        .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
          background-color: {background} !important;
          color: {text} !important;
          width: 100% !important;
          max-width: 100% !important;
          min-width: 0 !important;
          box-sizing: border-box !important;
        }}
        [data-testid="stMainBlockContainer"] {{
          width: 100% !important;
          min-width: 0 !important;
          box-sizing: border-box !important;
        }}
        [data-testid="stHeader"] {{ background-color: {background} !important; }}
        [data-testid="stSidebar"], [data-testid="stSidebarContent"] {{
          background-color: {sidebar} !important;
          color: {text} !important;
        }}
        .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5,
        .stApp p, .stApp label, .stApp [data-testid="stMarkdownContainer"],
        .stApp [data-testid="stMetricLabel"], .stApp [data-testid="stMetricValue"] {{
          color: {text} !important;
        }}
        .stApp div[data-testid="stForm"],
        .stApp div[data-testid="stVerticalBlockBorderWrapper"] {{
          background-color: {surface} !important;
          border-color: {border} !important;
        }}
        .stApp input, .stApp textarea,
        .stApp div[data-baseweb="select"] > div,
        .stApp div[data-baseweb="input"],
        .stApp div[data-baseweb="input"] > div,
        .stApp div[data-baseweb="base-input"],
        .stApp div[data-baseweb="textarea"] {{
          background-color: {field} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp div[data-baseweb="select"] > div > div:last-child,
        .stApp div[data-baseweb="select"] [aria-hidden="true"] {{
          background-color: {field} !important;
          color: {text} !important;
        }}
        .stApp [data-testid="stDateInput"] div[data-baseweb="input"],
        .stApp [data-testid="stDateInput"] div[data-baseweb="input"] > div,
        .stApp [data-testid="stDateInput"] input,
        .stApp [data-testid="stNumberInput"] div[data-baseweb="input"],
        .stApp [data-testid="stNumberInput"] div[data-baseweb="input"] > div,
        .stApp [data-testid="stNumberInput"] input,
        .stApp [data-testid="stTextInput"] div[data-baseweb="input"],
        .stApp [data-testid="stTextInput"] div[data-baseweb="input"] > div,
        .stApp [data-testid="stTextInput"] input,
        .stApp [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
        .stApp [data-testid="stSelectbox"] div[data-baseweb="select"] > div > div {{
          background-color: {field} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stDateInput"] > div,
        .stApp [data-testid="stDateInput"] > div > div,
        .stApp [data-testid="stDateInput"] div[data-baseweb="base-input"],
        .stApp [data-testid="stNumberInput"] div[data-baseweb="base-input"],
        .stApp [data-testid="stSelectbox"] div[data-baseweb="select"] > div > div:last-child {{
          background: {field} !important;
          background-color: {field} !important;
          color: {text} !important;
        }}
        .stApp [data-testid="stDateInput"] input,
        .stApp [data-testid="stNumberInput"] input,
        .stApp [data-testid="stTextInput"] input {{
          background: {field} !important;
          background-color: {field} !important;
          color: {text} !important;
          -webkit-text-fill-color: {text} !important;
          -webkit-box-shadow: 0 0 0 1000px {field} inset !important;
          box-shadow: 0 0 0 1000px {field} inset !important;
          caret-color: {text} !important;
        }}
        .stApp [data-testid="stDateInputField"],
        .stApp [data-testid="stDateInputFieldsScroller"],
        .stApp [data-testid="stDateInputField"] .react-aria-DateInput,
        .stApp [data-testid="stDateInputField"] [data-range-field],
        .stApp [data-testid="stDateInputField"] [role="spinbutton"] {{
          background: {field} !important;
          background-color: {field} !important;
          color: {text} !important;
          -webkit-text-fill-color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stDateInputField"] button,
        .stApp [data-testid="stDateInputField"] svg {{
          background: transparent !important;
          color: {text} !important;
          stroke: {text} !important;
        }}
        .stApp [data-testid="stDateInputCalendar"],
        .stApp [data-testid="stDateInputCalendar"] * {{
          background-color: {surface} !important;
          color: {text} !important;
        }}
        .stApp [data-testid="stSelectbox"] > div:last-child {{
          background: {field} !important;
          background-color: {field} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stSelectbox"] > div:last-child input,
        .stApp [data-testid="stSelectbox"] > div:last-child button {{
          background: transparent !important;
          color: {text} !important;
          -webkit-text-fill-color: {text} !important;
        }}
        .stApp [data-testid="stSelectbox"] div:has(> input[role="combobox"]),
        .stApp [data-testid="stSelectbox"] div:has(> input[aria-label]) {{
          background: {field} !important;
          background-color: {field} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stSelectbox"] input[role="combobox"],
        .stApp [data-testid="stSelectbox"] input[aria-label] {{
          background: transparent !important;
          background-color: transparent !important;
          color: {text} !important;
          -webkit-text-fill-color: {text} !important;
          caret-color: {text} !important;
          opacity: 1 !important;
        }}
        .stApp [data-testid="stSelectbox"] button[aria-label="Open"],
        .stApp [data-testid="stSelectbox"] button[aria-label="Clear value"] {{
          background: transparent !important;
          background-color: transparent !important;
          color: {text} !important;
          border-color: transparent !important;
          opacity: 1 !important;
        }}
        .stApp [data-testid="stSelectbox"] [data-disabled] {{
          color: {muted} !important;
          -webkit-text-fill-color: {muted} !important;
          opacity: 1 !important;
        }}
        .stApp input::placeholder, .stApp textarea::placeholder {{
          color: {muted} !important;
          opacity: 1 !important;
        }}
        .stApp [data-testid="stButton"] button,
        .stApp [data-testid="stFormSubmitButton"] button,
        .stApp [data-testid="stDownloadButton"] button,
        .stApp button[data-testid^="stBaseButton-secondary"],
        .stApp [data-testid="stSegmentedControl"] button {{
          background-color: {button} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stSegmentedControl"] > div,
        .stApp div[data-baseweb="button-group"] {{
          background-color: {button} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stButton"] button:hover,
        .stApp [data-testid="stFormSubmitButton"] button:hover,
        .stApp [data-testid="stDownloadButton"] button:hover,
        .stApp button[data-testid^="stBaseButton-secondary"]:hover,
        .stApp [data-testid="stSegmentedControl"] button:hover {{
          background-color: {hover} !important;
          color: {text} !important;
        }}
        .stApp [data-testid="stFormSubmitButton"] button[kind="primary"],
        .stApp button[data-testid="stBaseButton-primary"] {{
          background-color: #ff4b4b !important;
          color: #ffffff !important;
          border-color: #ff4b4b !important;
        }}
        .stApp [data-testid="stSegmentedControl"] button[aria-pressed="true"] {{
          background-color: {'#4b5060' if dark else '#e6e9ef'} !important;
          color: {text} !important;
        }}
        .stApp button p, .stApp button span,
        .stApp button [data-testid="stMarkdownContainer"] {{
          color: inherit !important;
        }}
        .stApp button svg {{
          color: inherit !important;
          fill: currentColor !important;
          opacity: 1 !important;
        }}
        .stApp [data-testid="stTooltipIcon"],
        .stApp [data-testid="stTooltipIcon"] button,
        .stApp [data-testid="stTooltipHoverTarget"] button {{
          background: transparent !important;
          color: {text} !important;
          border-color: transparent !important;
        }}
        .stApp [data-testid="stTooltipIcon"] svg,
        .stApp [data-testid="stTooltipIcon"] span,
        .stApp [data-testid="stTooltipHoverTarget"] button svg,
        .stApp [data-testid="stTooltipHoverTarget"] button span {{
          color: {text} !important;
          fill: none !important;
          stroke: {text} !important;
          opacity: 1 !important;
        }}
        .stApp [data-testid="stTooltipIcon"] svg *,
        .stApp [data-testid="stTooltipHoverTarget"] button svg * {{
          fill: none !important;
          stroke: {text} !important;
          opacity: 1 !important;
        }}
        .stApp [role="tooltip"] {{
          max-width: min(32rem, calc(100vw - 1rem)) !important;
          max-height: calc(100dvh - 1rem) !important;
        }}
        .stApp [data-testid="stTooltipContent"] {{
          max-width: min(32rem, calc(100vw - 1rem)) !important;
          max-height: calc(100dvh - 1rem) !important;
          overflow-x: hidden !important;
          overflow-y: auto !important;
          white-space: normal !important;
          overflow-wrap: anywhere !important;
          word-break: normal !important;
          background-color: {help_background} !important;
          color: {help_foreground} !important;
        }}
        .stApp [data-testid="stTooltipContent"] * {{
          color: {help_foreground} !important;
          white-space: normal !important;
          overflow-wrap: anywhere !important;
        }}
        .stApp div[data-baseweb="popover"] {{
          max-width: calc(100vw - 1rem) !important;
        }}
        .stApp div[data-baseweb="popover"] > div {{
          max-width: calc(100vw - 1rem) !important;
          overflow-wrap: anywhere !important;
        }}
        #vg-tooltip-element, .vg-tooltip {{
          max-width: min(24rem, calc(100vw - 1rem)) !important;
          pointer-events: none !important;
          overflow-wrap: anywhere !important;
        }}
        .stApp [data-testid="stDateInput"] button,
        .stApp [data-testid="stNumberInput"] button {{
          background-color: {field} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stDateInput"] svg,
        .stApp [data-testid="stNumberInput"] svg,
        .stApp div[data-baseweb="select"] svg {{
          color: {text} !important;
          fill: currentColor !important;
        }}
        .stApp button:disabled {{
          background-color: {'#242731' if dark else '#f3f4f6'} !important;
          color: {'#a6abb4' if dark else '#9ca3af'} !important;
          opacity: 1 !important;
        }}
        [data-testid="stSidebar"] a,
        [data-testid="stSidebar"] a span,
        [data-testid="stSidebar"] [data-testid="stIconMaterial"],
        [data-testid="stSidebar"] .material-symbols-rounded {{
          color: {text} !important;
          opacity: 1 !important;
        }}
        [data-testid="stSidebar"] svg {{
          color: {text} !important;
          fill: currentColor !important;
          opacity: 1 !important;
        }}
        [data-testid="stSidebarNav"] a:hover {{
          background-color: {hover} !important;
        }}
        .stApp [data-testid="stExpander"] details,
        .stApp [data-testid="stExpander"] summary,
        .stApp div[data-baseweb="popover"] > div,
        .stApp div[role="listbox"],
        .stApp div[data-baseweb="calendar"],
        .stApp div[data-baseweb="menu"],
        .stApp [data-testid="stFileUploaderDropzone"],
        .stApp [data-testid="stCameraInput"] > div {{
          background-color: {surface} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp div[data-baseweb="calendar"] *,
        .stApp div[data-baseweb="menu"] *,
        .stApp div[role="listbox"] * {{
          color: {text} !important;
        }}
        .stApp div[role="option"] {{
          background-color: {surface} !important;
          color: {text} !important;
        }}
        .stApp div[role="option"]:hover,
        .stApp div[role="option"][aria-selected="true"] {{
          background-color: {hover} !important;
        }}
        .stApp [data-testid="stDataFrame"],
        .stApp [data-testid="stTable"] {{
          background-color: {field} !important;
          color: {text} !important;
          border-color: {border} !important;
        }}
        .stApp [data-testid="stDataFrame"] canvas {{
          background-color: {field} !important;
        }}
        .stApp hr {{ border-color: {border} !important; }}
        iframe[title="Χειρισμός πλαϊνού μενού με αφή"],
        iframe[title="Αυτόματο κλείσιμο πλαϊνού μενού"],
        iframe[title="Επαναφορά επιλογής εμφάνισης"],
        iframe[title="Αποθήκευση επιλογής εμφάνισης"],
        iframe[title="Αποθήκευση ασφαλούς σύνδεσης"],
        iframe[title="Διαγραφή αποθηκευμένης σύνδεσης"],
        iframe[title="Ανάκτηση σύνδεσης κινητού"],
        iframe[title="Κλείσιμο γραφήματος με αφή"] {{
          position: fixed !important;
          left: -10000px !important;
          top: -10000px !important;
          width: 1px !important;
          height: 1px !important;
          opacity: 0 !important;
          pointer-events: none !important;
          border: 0 !important;
        }}
        div[data-testid="stElementContainer"]:has(iframe[title="Χειρισμός πλαϊνού μενού με αφή"]),
        div[data-testid="stElementContainer"]:has(iframe[title="Αυτόματο κλείσιμο πλαϊνού μενού"]),
        div[data-testid="stElementContainer"]:has(iframe[title="Επαναφορά επιλογής εμφάνισης"]),
        div[data-testid="stElementContainer"]:has(iframe[title="Αποθήκευση επιλογής εμφάνισης"]),
        div[data-testid="stElementContainer"]:has(iframe[title="Αποθήκευση ασφαλούς σύνδεσης"]),
        div[data-testid="stElementContainer"]:has(iframe[title="Διαγραφή αποθηκευμένης σύνδεσης"]),
        div[data-testid="stElementContainer"]:has(iframe[title="Ανάκτηση σύνδεσης κινητού"]),
        div[data-testid="stElementContainer"]:has(iframe[title="Κλείσιμο γραφήματος με αφή"]) {{
          display: block !important;
          position: relative !important;
          height: 0 !important;
          min-height: 0 !important;
          overflow: visible !important;
          margin: 0 !important;
          padding: 0 !important;
        }}
        @media screen and (max-width: 640px) {{
          [role="tooltip"],
          [data-testid="stTooltipContent"] {{
            width: min(28rem, calc(100dvw - 3rem)) !important;
            min-width: 0 !important;
            max-width: calc(100dvw - 3rem) !important;
            max-height: calc(100dvh - 1rem) !important;
            overflow-x: hidden !important;
            overflow-y: auto !important;
            white-space: normal !important;
            overflow-wrap: anywhere !important;
          }}
          div[data-baseweb="popover"]:has([role="tooltip"]),
          div[data-baseweb="popover"]:has([data-testid="stTooltipContent"]) {{
            width: min(28rem, calc(100dvw - 3rem)) !important;
            min-width: 0 !important;
            max-width: calc(100dvw - 3rem) !important;
          }}
        }}
        @media screen and (orientation: landscape) and (pointer: coarse),
               screen and (orientation: landscape) and (max-height: 640px) {{
          html, body, #root, .stApp,
          [data-testid="stAppViewContainer"],
          [data-testid="stMain"],
          [data-testid="stMainBlockContainer"] {{
            width: 100% !important;
            max-width: 100% !important;
            min-width: 0 !important;
            box-sizing: border-box !important;
          }}
          [data-testid="stMain"] {{
            margin-left: 0 !important;
          }}
          [data-testid="stAppViewContainer"] {{
            overflow-x: hidden !important;
          }}
          [data-testid="stMainBlockContainer"] {{
            padding-left: max(.75rem, env(safe-area-inset-left)) !important;
            padding-right: max(.75rem, env(safe-area-inset-right)) !important;
            padding-bottom: max(1rem, env(safe-area-inset-bottom)) !important;
          }}
          [data-testid="stSidebar"] {{
            position: fixed !important;
            top: 0 !important;
            bottom: 0 !important;
            left: 0 !important;
            height: 100dvh !important;
            z-index: 1000000 !important;
            max-width: min(22rem, 46vw) !important;
          }}
          [role="tooltip"],
          [data-testid="stTooltipContent"] {{
            width: min(28rem, calc(100dvw - 3rem)) !important;
            max-width: calc(100dvw - 3rem) !important;
            max-height: calc(100dvh - 1rem) !important;
          }}
          div[data-baseweb="popover"]:has([role="tooltip"]),
          div[data-baseweb="popover"]:has([data-testid="stTooltipContent"]) {{
            width: min(28rem, calc(100dvw - 3rem)) !important;
            min-width: 0 !important;
            max-width: calc(100dvw - 3rem) !important;
          }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def display_theme_control():
    current = selected_display_theme()
    choice = st.sidebar.segmented_control(
        "Φόντο", ["Φωτεινό", "Σκοτεινό"], default=current,
        key="display_theme_control",
    ) or current
    query_value = "dark" if choice == "Σκοτεινό" else "light"
    if st.query_params.get("display_theme") != query_value:
        st.query_params["display_theme"] = query_value
    persist_display_theme_in_browser(query_value)


def searchbox_theme_overrides():
    if selected_display_theme() != "Σκοτεινό":
        return None
    return {
        "wrapper": {
            "color": "#fafafa",
            "backgroundColor": "#1b1f27",
        },
        "clear": {"fill": "#c2c7d0", "stroke": "#c2c7d0"},
        "dropdown": {"fill": "#fafafa"},
        "searchbox": {
            "control": {
                "backgroundColor": "#31333f",
                "borderColor": "#555b66",
                "color": "#fafafa",
            },
            "input": {"color": "#fafafa"},
            "singleValue": {"color": "#fafafa"},
            "placeholder": {"color": "#c2c7d0"},
            "menuList": {"backgroundColor": "#262730", "color": "#fafafa"},
            "option": {
                "color": "#fafafa",
                "backgroundColor": "#262730",
                "highlightColor": "#3a3d49",
            },
        },
    }


def secret(name, default=""):
    try:
        return str(st.secrets.get(name, os.environ.get(name, default)))
    except FileNotFoundError:
        return os.environ.get(name, default)


restore_display_theme_from_browser()
apply_display_theme(selected_display_theme())

# A valid remembered session already exists before login_diary() is called.
# Loading its unread rows here lets the notification bubble render physically
# above the brand logo. Failure is deliberately silent until the user opens the
# Notifications page, so deploying Python before the SQL migration is safe.
notification_preview = []
preview_diary = None
if st.session_state.get("diary_auth"):
    try:
        preview_diary = Diary(
            secret("SUPABASE_URL"), secret("SUPABASE_PUBLISHABLE_KEY"),
            st.session_state,
        )
        notification_preview = load_notifications(preview_diary, quiet=True)
    except DiaryError:
        preview_diary = None
        notification_preview = []
if preview_diary is not None:
    notification_banner(preview_diary, notification_preview)
render_brand_header()
st.sidebar.caption("Έκδοση " + APP_VERSION)

if not st.session_state.get("diary_auth"):
    install_mobile_browser_behaviors(close_sidebar_on_mount=True)


@st.cache_data(ttl=86400, max_entries=2000, show_spinner=False)
def cached_open_food_facts_product(barcode, user_agent):
    """Share repeated barcode lookups across sessions to respect API limits."""
    return open_food_facts_product(barcode, user_agent)


@st.cache_data(ttl=86400, max_entries=1000, show_spinner=False)
def cached_dsld_search(query, api_key, limit=15):
    """Cache NIH DSLD name searches to stay well below its public API limit."""
    return dsld_search(query, api_key, limit)


@st.cache_data(ttl=86400, max_entries=2000, show_spinner=False)
def cached_dsld_label(dsld_id, api_key):
    return dsld_label(dsld_id, api_key)


@st.cache_data(ttl=86400, max_entries=1000, show_spinner=False)
def cached_supplement_name_search(query, user_agent, dsld_api_key, limit=20):
    """Search OFF and NIH independently, preserving useful partial results."""
    provider_limit = max(5, min(10, int(limit)))
    products, warnings = [], []
    try:
        products.extend(open_food_facts_supplement_search(
            query, user_agent, provider_limit
        ))
    except ServiceError:
        warnings.append("Open Food Facts")
    try:
        dsld_products = dsld_search(query, dsld_api_key, provider_limit)
    except ServiceError:
        warnings.append("NIH DSLD")
        dsld_products = []
    products = merge_external_supplement_results(
        products, dsld_products, limit=limit
    )
    return {"products": products, "unavailable": warnings}


@st.cache_data(ttl=86400, max_entries=2000, show_spinner=False)
def cached_supplement_barcode_product(barcode, user_agent, dsld_api_key):
    """Prefer the supplement label source, then use OFF as a fallback."""
    try:
        return dsld_product_by_barcode(barcode, dsld_api_key)
    except ServiceError:
        try:
            return open_food_facts_product(barcode, user_agent)
        except ServiceError:
            raise ServiceError(
                "Το προϊόν δεν βρέθηκε ούτε στο NIH DSLD ούτε στο Open Food Facts."
            ) from None


@st.cache_data(ttl=3600, max_entries=1000, show_spinner=False)
def cached_database_food_search(query, usda_api_key):
    """Search USDA, CoFID and the trial Greek-cheese subset."""
    external = search_food(query, usda_api_key, limit=30)
    greek = search_greek_cheeses(query, limit=15)
    return [*greek, *external]


def food_suggestions(search_term, usda_api_key, personal_foods,
                     shared_barcode_foods=None):
    """Return live results from history and all enabled food databases."""
    if len("".join(character for character in search_term if character.isalnum())) < 2:
        return []
    try:
        personal_candidates = search_personal_foods(search_term, personal_foods)
        for candidate in personal_candidates:
            if str(candidate.get("details") or "").startswith("Αποθηκευμένη συνταγή"):
                candidate["source"] = "Προσωπική συνταγή"
                candidate["dataType"] = "Υπολογισμός από υλικά"
        shared_candidates = search_shared_barcode_foods(
            search_term, shared_barcode_foods or [], limit=8
        )
        candidates = [*personal_candidates, *shared_candidates,
                      *cached_database_food_search(search_term, usda_api_key)]
    except ServiceError as error:
        return [("Δεν ήταν δυνατή η αναζήτηση · " + str(error), {"_error": str(error)})]
    candidates.sort(key=lambda item: (-item.get("search_score", 0), len(item.get("description", ""))))
    suggestions, seen = [], set()
    greek_search = bool(re.search(r"[α-ωάέήίόύώϊϋΐΰ]", search_term.lower()))
    for food in candidates:
        identity = (food.get("source"), food.get("description", "").casefold())
        if identity in seen:
            continue
        seen.add(identity)
        marker = ("🍲 " if food.get("source") == "Προσωπική συνταγή"
                  else "↻ " if food.get("source") == "Προσωπικό ιστορικό" else "")
        greek_prefix = (search_term.strip() + " → "
                        if greek_search and food.get("source") not in
                        ("Προσωπικό ιστορικό", "Προσωπική συνταγή",
                         "Ελληνικοί Πίνακες") else "")
        label = f"{marker}{greek_prefix}{food['description']} · {food['source']}"
        if food.get("dataType") and food.get("source") != "Προσωπικό ιστορικό":
            label += " · " + food["dataType"]
        suggestions.append((label, food))
        if len(suggestions) == 20:
            break
    return suggestions


def clear_calculation_state():
    for key in ("output", "output_signature", "meal_id"):
        st.session_state.pop(key, None)
    st.session_state.meal_saved = False


def food_selection_identity(food):
    """Return a stable identity for a database/history search result."""
    if not isinstance(food, dict):
        return ""
    return "|".join(str(food.get(field) or "").strip() for field in (
        "source", "source_id", "description",
    ))


def persist_food_editor_state():
    """Copy current widget values into food rows before their keys change.

    Food editor keys include the current epoch and list index. Deleting or
    expanding a row intentionally changes those keys. Without this snapshot,
    edited values can fall back to the original history/database defaults on
    the following rerun.
    """
    foods = st.session_state.get("foods", [])
    epoch = st.session_state.get("epoch", 0)
    for index, item in enumerate(foods):
        prefix = f"{epoch}_{index}"
        name_key = prefix + "name"
        amount_key = prefix + "amount"
        unit_key = prefix + "unit"
        density_key = prefix + "density"
        nova_key = prefix + "nova_group"
        medas_key = prefix + "medas_categories"

        if name_key in st.session_state:
            item["name"] = st.session_state[name_key]
        if amount_key in st.session_state:
            try:
                item["grams"] = float(st.session_state[amount_key])
            except (TypeError, ValueError):
                pass
        if unit_key in st.session_state:
            item["quantity_unit"] = (
                "ml" if st.session_state[unit_key] == "ml" else "g"
            )
        if density_key in st.session_state:
            try:
                item["density"] = float(st.session_state[density_key])
            except (TypeError, ValueError):
                pass
        if nova_key in st.session_state:
            previous_nova = normalize_nova(item.get("nova_group"))
            selected_nova = nova_from_option(st.session_state[nova_key])
            previous_source = str(item.get("nova_source") or "")
            item["nova_group"] = selected_nova
            item["nova_source"] = (
                previous_source
                if selected_nova and selected_nova == previous_nova and previous_source
                else "Επιλογή χρήστη" if selected_nova else ""
            )
            if selected_nova != previous_nova:
                for metadata_key in (
                    "nova_status", "upf_markers", "additive_functions", "nova_reason",
                    "classification_method", "classification_version", "source_reference",
                ):
                    item.pop(metadata_key, None)
                if selected_nova:
                    item["nova_status"] = "uncertain"
                    item["nova_reason"] = "Χειροκίνητη επιλογή χρήστη χωρίς τεκμηρίωση από τον οδηγό."
                    item["classification_method"] = "user-selected"
        if medas_key in st.session_state:
            previous_categories = normalize_medas_categories(
                item.get("medas_categories")
            )
            previous_source = str(item.get("medas_source") or "")
            categories = medas_categories_from_options(st.session_state[medas_key])
            item["medas_categories"] = categories
            item["medas_source"] = (
                previous_source
                if categories == previous_categories and previous_source
                else "Επιλογή χρήστη" if categories else ""
            )

        component_prefix = prefix + "medas_component_amount_"
        component_values = {}
        component_widgets_found = False
        for key, value in list(st.session_state.items()):
            if not str(key).startswith(component_prefix):
                continue
            component_widgets_found = True
            category_and_token = str(key)[len(component_prefix):]
            category = category_and_token.rsplit("_", 1)[0]
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue
            if numeric_value > 0:
                component_values[category] = numeric_value
        if component_widgets_found:
            item["medas_component_amounts"] = normalize_medas_component_amounts(
                component_values
            )


def reset_meal(clear_recipe_context=True):
    """Start a clean meal only after the previous one has been saved."""
    st.session_state.foods = []
    st.session_state.results = {}
    st.session_state.uncertainty = ""
    st.session_state.input_mode = ""
    st.session_state.epoch = st.session_state.get("epoch", 0) + 1
    for key in ("output", "output_signature", "meal_id", "barcode_product",
                "barcode_lookup_error"):
        st.session_state.pop(key, None)
    st.session_state.meal_saved = False
    if clear_recipe_context:
        st.session_state.pop("recipe_edit_context", None)


diary = login_diary(secret("SUPABASE_URL"), secret("SUPABASE_PUBLISHABLE_KEY"),
                    secret("COURSE_SIGNUP_ENABLED", "false").lower() == "true")
if diary:
    display_theme_control()
    profile = require_profile(diary)
    meal_page = st.Page(
        lambda: None, title="Νέο γεύμα", icon=":material/add_circle:", default=True
    )
    recipe_page = st.Page(
        lambda: recipe_dashboard(diary, meal_page), title="Συνταγές",
        icon=":material/menu_book:", url_path="recipes",
    )
    diary_page = st.Page(
        lambda: history(diary), title="Ημερολόγιο", icon=":material/calendar_month:",
        url_path="diary",
    )
    weight_page = st.Page(
        lambda: weight_dashboard(diary, profile), title="Καταγραφή βάρους",
        icon=":material/monitor_weight:", url_path="weight",
    )
    period_page = st.Page(
        lambda: period_dashboard(diary, profile), title="Ανάλυση περιόδου",
        icon=":material/date_range:", url_path="period",
    )
    nutrient_page = st.Page(
        lambda: nutrient_dashboard(diary, profile), title="Ημερήσια πρόσληψη",
        icon=":material/donut_large:", url_path="daily-intake",
    )
    supplement_page = st.Page(
        lambda: supplement_dashboard(
            diary,
            lambda barcode: cached_supplement_barcode_product(
                barcode,
                secret(
                    "OPENFOODFACTS_USER_AGENT",
                    "NutritionDiaryHMU/1.0 (https://github.com/nthalassinos/Nutrition-app)",
                ),
                secret("DSLD_API_KEY"),
            ),
            lambda query, limit=20: cached_supplement_name_search(
                query,
                secret(
                    "OPENFOODFACTS_USER_AGENT",
                    "NutritionDiaryHMU/1.0 (https://github.com/nthalassinos/Nutrition-app)",
                ),
                secret("DSLD_API_KEY"), limit,
            ),
            lambda dsld_id: cached_dsld_label(dsld_id, secret("DSLD_API_KEY")),
            lambda barcode: cached_open_food_facts_product(
                barcode,
                secret(
                    "OPENFOODFACTS_USER_AGENT",
                    "NutritionDiaryHMU/1.0 (https://github.com/nthalassinos/Nutrition-app)",
                ),
            ),
        ),
        title="Συμπληρώματα", icon=":material/medication:",
        url_path="supplements",
    )
    product_capture_page = st.Page(
        lambda: product_capture_dashboard(
            diary,
            lambda barcode: cached_open_food_facts_product(
                barcode,
                secret(
                    "OPENFOODFACTS_USER_AGENT",
                    "NutritionDiaryHMU/1.0 (https://github.com/nthalassinos/Nutrition-app)",
                ),
            ),
        ),
        title="Καταγραφή προϊόντων", icon=":material/inventory_2:",
        url_path="products",
    )
    medas_page = st.Page(
        lambda: medas_dashboard(diary), title="MEDAS",
        icon=":material/favorite:", url_path="medas",
    )
    upf_page = st.Page(
        lambda: upf_dashboard(diary), title="UPF / NOVA", icon=":material/monitoring:",
        url_path="upf-nova",
    )
    profile_page = st.Page(
        lambda: profile_form(diary, profile), title="Προφίλ μου", icon=":material/person:",
        url_path="profile",
    )
    backup_page = st.Page(
        lambda: backup_dashboard(diary, profile), title="Αντίγραφο ασφαλείας",
        icon=":material/backup:", url_path="backup",
    )
    current_notifications = (
        notification_preview
        if preview_diary is not None
        else load_notifications(diary, quiet=True)
    )
    current_unread = unread_count(current_notifications)
    notifications_page = st.Page(
        lambda: notifications_dashboard(diary),
        title=(f"Ειδοποιήσεις ({current_unread})"
               if current_unread else "Ειδοποιήσεις"),
        icon=":material/notifications:", url_path="notifications",
    )
    selected_page = st.navigation(
        [meal_page, diary_page, nutrient_page, period_page, medas_page, upf_page,
         weight_page, supplement_page, product_capture_page, recipe_page,
         notifications_page, profile_page, backup_page],
        position="sidebar"
    )
    if st.session_state.pop("open_notifications_requested", False):
        st.switch_page(notifications_page)
    install_mobile_browser_behaviors()
    selected_page.run()
    if selected_page.url_path:
        st.stop()
else:
    st.info("Το προσωπικό ημερολόγιο θα ενεργοποιηθεί όταν συνδεθεί το Supabase. Προς το παρόν οι υπολογισμοί δεν αποθηκεύονται.")

password = "" if diary else secret("APP_PASSWORD")
if password and not st.session_state.get("authorized"):
    st.title("Ανάλυση γεύματος")
    with st.form("login"):
        entered = st.text_input("Κωδικός μαθήματος", type="password")
        if st.form_submit_button("Είσοδος"):
            if hmac.compare_digest(entered.encode(), password.encode()):
                st.session_state.authorized = True
                st.rerun()
            else:
                st.error("Ο κωδικός δεν είναι σωστός.")
    st.stop()

recipe_edit_context = st.session_state.get("recipe_edit_context")
if recipe_edit_context:
    editing_copy = recipe_edit_context.get("mode") == "copy"
    st.title("Αντίγραφο συνταγής" if editing_copy else "Επεξεργασία συνταγής")
    st.info(
        ("Επεξεργάζεστε αντίγραφο της συνταγής " if editing_copy
         else "Επεξεργάζεστε τη συνταγή ")
        + f"«{recipe_edit_context.get('original_title', '')}». "
        + "Αλλάξτε ποσότητες, διαγράψτε ή προσθέστε υλικά και υπολογίστε ξανά "
          "τα θρεπτικά στοιχεία πριν από την αποθήκευση. Οι παλιές καταγραφές "
          "του ημερολογίου δεν αλλάζουν."
    )
    if st.button(
        "Ακύρωση επεξεργασίας", icon=":material/close:", type="secondary"
    ):
        reset_meal()
        st.switch_page(recipe_page)
else:
    st.title("Ανάλυση γεύματος")
    st.caption("Φωτογραφία • επιβεβαίωση μερίδας • θρεπτικά στοιχεία")
if st.session_state.pop("meal_saved_notice", None):
    st.success("Το γεύμα αποθηκεύτηκε επιτυχώς. Μπορείτε να ξεκινήσετε νέο γεύμα.")
if diary and not recipe_edit_context:
    meal_progress(diary)
    compact_nutrient_summary(diary, profile, nutrient_page)
if diary and not recipe_edit_context:
    try:
        st.session_state.photo_usage_today = diary.photo_usage()
        st.caption(f"Σήμερα χρησιμοποιήσατε {st.session_state.photo_usage_today}/8 αναλύσεις φωτογραφίας.")
    except DiaryError as error:
        st.error(str(error))
        st.stop()
openai_key, usda_key = secret("OPENAI_API_KEY"), secret("USDA_API_KEY")
if not usda_key:
    st.warning("Ο διαχειριστής πρέπει να προσθέσει USDA_API_KEY στις ρυθμίσεις της εφαρμογής.")
if not openai_key:
    st.caption("Η φωτογραφική αναγνώριση θα ενεργοποιηθεί όταν προστεθεί OPENAI_API_KEY.")

for name, default in [("foods", []), ("results", {}), ("epoch", 0), ("input_mode", "")]:
    if name not in st.session_state:
        st.session_state[name] = default


def choose_input_mode(mode):
    if not st.session_state.get("meal_saved", False):
        st.session_state.input_mode = mode


def add_search_food():
    if st.session_state.get("meal_saved", False):
        return
    # Another browser/device using the same account may have saved a new food
    # since this Streamlit session loaded its history. Refresh from Supabase
    # whenever a new search card is opened instead of keeping a stale list.
    st.session_state.personal_foods_dirty = True
    st.session_state.input_mode = "search"
    st.session_state.foods.append({"name": "", "query": "", "grams": 100., "note": ""})
    clear_calculation_state()


# Continue cleanly if an older deployed version left a saved meal locked in this session.
if st.session_state.get("meal_saved", False):
    reset_meal()


personal_foods = []
shared_barcode_foods = []
if diary:
    if st.session_state.get("personal_foods_cache_version") != PERSONAL_FOODS_CACHE_VERSION:
        st.session_state.pop("personal_foods", None)
        st.session_state.personal_foods_cache_version = PERSONAL_FOODS_CACHE_VERSION
    if ("personal_foods" not in st.session_state
            or st.session_state.get("personal_foods_dirty", False)):
        try:
            # Separately stored recipes are searched before ordinary history.
            # A same-title recipe therefore cannot be hidden by an older meal.
            recipes = diary.personal_recipes()
            try:
                registered_products = diary.user_products()
            except DiaryError:
                # The meal page remains available during a rolling deployment
                # or before the optional product table has been installed.
                registered_products = []
            history_foods = diary.recent_foods()
            preferred_names = {
                str(item.get("name") or "").casefold()
                for item in [*recipes, *registered_products]
            }
            st.session_state.personal_foods = [
                *recipes,
                *registered_products,
                *(item for item in history_foods
                  if str(item.get("name") or "").casefold() not in preferred_names),
            ]
            st.session_state.personal_foods_dirty = False
        except DiaryError as error:
            st.session_state.personal_foods = []
            st.caption("Δεν φορτώθηκαν οι παλαιότερες καταχωρίσεις τροφίμων: " + str(error))
    personal_foods = st.session_state.get("personal_foods", [])
    if ("shared_barcode_foods" not in st.session_state
            or st.session_state.get("shared_barcode_foods_dirty", False)):
        try:
            st.session_state.shared_barcode_foods = diary.shared_barcode_products()
            st.session_state.shared_barcode_foods_dirty = False
        except DiaryError:
            # The rest of food search and barcode lookup remain available if
            # the optional shared-barcode table has not been installed yet.
            st.session_state.shared_barcode_foods = []
    shared_barcode_foods = st.session_state.get("shared_barcode_foods", [])


meal_locked = st.session_state.get("meal_saved", False)
search_choice, photo_choice, barcode_choice = st.columns(3)
search_choice.button(
    "Αναζήτηση", icon=":material/search:", width="stretch", wrap=True,
    type="primary" if st.session_state.input_mode == "search" else "secondary",
    on_click=add_search_food, key="choose_search", disabled=meal_locked,
)
photo_choice.button(
    "Φωτογραφία", icon=":material/photo_camera:", width="stretch", wrap=True,
    type="primary" if st.session_state.input_mode == "photo" else "secondary",
    on_click=choose_input_mode, args=("photo",), key="choose_photo", disabled=meal_locked,
)
barcode_choice.button(
    "Barcode", icon=":material/barcode_scanner:", width="stretch", wrap=True,
    type="primary" if st.session_state.input_mode == "barcode" else "secondary",
    on_click=choose_input_mode, args=("barcode",), key="choose_barcode", disabled=meal_locked,
)

input_mode = st.session_state.input_mode
if input_mode == "photo" and not meal_locked:
    source = st.radio("Φωτογραφία", ["Αρχείο", "Κάμερα"], horizontal=True)
    photo = st.file_uploader("Επιλέξτε φωτογραφία γεύματος", type=["jpg", "jpeg", "png", "webp"]) if source == "Αρχείο" else st.camera_input("Φωτογραφίστε το γεύμα")
    if photo:
        st.image(photo, width=300)
    note = st.text_input("Περιγραφή / πρόσθετα υλικά (προαιρετικά)", max_chars=2000)
    st.caption(
        "Με το πάτημα «Αναγνώριση τροφίμων» η φωτογραφία αποστέλλεται στο OpenAI "
        "για επεξεργασία. Η φωτογραφία δίνει εκτίμηση· επιβεβαιώστε τα γραμμάρια, "
        "το μαγείρεμα και πρόσθετα υλικά, όπως λάδι."
    )
    if st.button("Αναγνώριση τροφίμων", type="primary", disabled=not (photo and openai_key)):
        if photo.size > 10 * 1024 * 1024:
            st.error("Χρησιμοποιήστε φωτογραφία έως 10 MB.")
        else:
            try:
                if diary:
                    st.session_state.photo_usage_today = diary.reserve_photo()
                with st.spinner("Αναγνώριση γεύματος…"):
                    data = recognize(photo.getvalue(), openai_key, secret("OPENAI_MODEL", "gpt-4.1-mini"), note)
                matched_foods = apply_best_food_matches(
                    data["foods"], lambda query: cached_database_food_search(query, usda_key))
                persist_food_editor_state()
                st.session_state.foods.extend(data["foods"])
                st.session_state.results = {}
                clear_calculation_state()
                st.session_state.epoch += 1
                st.session_state.uncertainty = data["uncertainty"]
                if data["foods"] and matched_foods < len(data["foods"]):
                    missing = len(data["foods"]) - matched_foods
                    extra_note = (f"Για {missing} αναγνωρισμένο τρόφιμο δεν επιλέχθηκε αυτόματα "
                                  "εγγραφή βάσης· επιλέξτε μία από την αναζήτηση της καρτέλας.")
                    st.session_state.uncertainty = " ".join(
                        part for part in [st.session_state.uncertainty, extra_note] if part)
                if diary:
                    st.success(f"Απομένουν {8 - st.session_state.photo_usage_today} αναλύσεις φωτογραφίας για σήμερα.")
            except ServiceError as error:
                st.error(str(error))
            except Exception:
                st.error("Δεν ήταν δυνατή η ανάγνωση της φωτογραφίας. Δοκιμάστε αρχείο JPG ή PNG.")
elif input_mode == "barcode" and not meal_locked:
    st.caption("Σαρώστε το barcode ή πληκτρολογήστε τα ψηφία.")
    barcode_source = st.radio(
        "Εισαγωγή barcode", ["Ζωντανή σάρωση", "Πληκτρολόγηση"],
        horizontal=True, key="food_barcode_source",
    )
    typed_barcode = ""
    barcode_submit = False
    lookup_code = None
    if barcode_source == "Ζωντανή σάρωση":
        lookup_code = live_barcode_scanner(
            key="food_live_barcode_scanner",
            disabled=bool(st.session_state.get("barcode_product")),
        )
    else:
        known_barcode_options = barcode_history_options(
            personal_foods, st.session_state.get("session_barcodes", {}),
            shared_barcode_foods,
        )
        # A form submits the current field value together with the button tap. This is
        # reliable on mobile browsers where the first tap may otherwise only close the keyboard.
        with st.form("typed_barcode_form", clear_on_submit=False):
            typed_barcode = st.selectbox(
                "Αριθμός barcode",
                known_barcode_options,
                index=None,
                key="typed_barcode",
                placeholder="Πληκτρολογήστε ή επιλέξτε προηγούμενο barcode",
                accept_new_options=True,
                filter_mode="prefix",
            ) or ""
            barcode_submit = st.form_submit_button("Αναζήτηση αριθμού barcode", type="primary")
    if barcode_source == "Πληκτρολόγηση" and barcode_submit:
        lookup_code = barcode_from_option(typed_barcode)

    if lookup_code is not None:
        st.session_state.pop("barcode_product", None)
        st.session_state.pop("barcode_lookup_error", None)
        try:
            shared_fallback = None
            if diary:
                try:
                    shared_fallback = diary.shared_barcode_product(lookup_code)
                except DiaryError:
                    # The optional shared table must never block normal lookup.
                    pass
            with st.spinner("Αναζήτηση προϊόντος barcode…"):
                try:
                    product = cached_open_food_facts_product(
                        lookup_code,
                        secret(
                            "OPENFOODFACTS_USER_AGENT",
                            "NutritionDiaryHMU/1.0 (https://github.com/nthalassinos/Nutrition-app)",
                        ),
                    )
                except ServiceError:
                    if not shared_fallback:
                        raise
                    product = shared_fallback
                st.session_state.barcode_product = product
                st.session_state.setdefault("session_barcodes", {})[
                    product["barcode"]
                ] = product["name"]
            if diary and product.get("database_source") == "Open Food Facts":
                try:
                    diary.save_shared_barcode_product(product)
                    # Make the newly contributed product searchable immediately,
                    # without waiting for a new browser session.
                    replacement = {
                        "barcode": product["barcode"],
                        "product_name": product["name"],
                        "nutrients": product["nutrients"],
                        "basis_unit": product.get("basis_unit", "g"),
                        "search_text": product.get("search_text", product["name"]),
                        "source": "Open Food Facts",
                    }
                    current_shared = list(st.session_state.get("shared_barcode_foods", []))
                    current_shared = [
                        row for row in current_shared
                        if str(row.get("barcode")) != str(product["barcode"])
                    ]
                    st.session_state.shared_barcode_foods = [replacement, *current_shared]
                except DiaryError:
                    pass
        except ServiceError as error:
            st.session_state.pop("barcode_product", None)
            st.session_state.barcode_lookup_error = str(error)
    if st.session_state.get("barcode_lookup_error"):
        st.error(st.session_state.barcode_lookup_error)
    product = st.session_state.get("barcode_product")
    if product:
        st.success(f"Βρέθηκε: {product['name']}")
        product_source = product.get("database_source") or "Open Food Facts"
        st.caption(f"Barcode {product['barcode']} · Πηγή: {product_source}")
        if product.get("nova_group"):
            st.info(nova_short_label(product["nova_group"]) + " · Πηγή ταξινόμησης: Open Food Facts")
        else:
            st.warning(
                "Δεν υπάρχει διαθέσιμη κατάταξη NOVA για αυτό το προϊόν. "
                "Θα μπορέσετε να την επιλέξετε στην καρτέλα του τροφίμου."
            )
        if product.get("ingredients_text") or product.get("additives"):
            with st.expander("Στοιχεία ετικέτας για έλεγχο της κατάταξης"):
                if product.get("ingredients_text"):
                    st.write(product["ingredients_text"])
                if product.get("additives"):
                    st.caption("Πρόσθετα: " + ", ".join(product["additives"]))
        if st.button("Χρήση αυτού του προϊόντος", type="primary"):
            persist_food_editor_state()
            off_nova_group = normalize_nova(product.get("nova_group"))
            st.session_state.foods.append({"name": product["name"], "query": "", "grams": 100.,
                "note": "Ελέγξτε την ετικέτα και διορθώστε την ποσότητα που καταναλώθηκε.",
                "direct_nutrients": product["nutrients"],
                "quantity_unit": product["basis_unit"], "basis_unit": product["basis_unit"],
                "database_entry": product_source + " · barcode " + product["barcode"],
                "nova_group": off_nova_group,
                "nova_source": product.get("nova_source", ""),
                "nova_status": "probable" if off_nova_group else "unclassified",
                "nova_reason": (
                    "Κατάταξη NOVA που παρέχεται από το Open Food Facts. "
                    "Χρειάζεται έλεγχος της πλήρους ετικέτας για επιβεβαίωση."
                    if off_nova_group else ""
                ),
                "classification_method": "database" if off_nova_group else "",
                "classification_version": "Open Food Facts API v3" if off_nova_group else "",
                "source_reference": "Open Food Facts" if off_nova_group else "",
                "ingredient_list": product.get("ingredients_text", ""),
                "ingredients_text": product.get("ingredients_text", ""),
                "additives": product.get("additives", []),
                "brand": product.get("brand", ""),
                "barcode": product.get("barcode", "")})
            clear_calculation_state()
            st.session_state.epoch += 1
            st.session_state.pop("barcode_product", None)
            st.session_state.pop("barcode_lookup_error", None)
            st.rerun()
if st.session_state.get("uncertainty"):
    st.warning(st.session_state.uncertainty)

rows = []
for index, item in enumerate(st.session_state.foods):
    prefix = f"{st.session_state.epoch}_{index}"
    current_title = st.session_state.get(prefix + "name", item.get("name", "")) or "Νέο τρόφιμο"
    card_column, delete_column = st.columns([0.91, 0.09], vertical_alignment="top")
    with delete_column:
        if st.button("🗑️", key=prefix + "delete", help="Διαγραφή τροφίμου",
                     disabled=meal_locked):
            persist_food_editor_state()
            st.session_state.foods.pop(index)
            st.session_state.epoch += 1
            clear_calculation_state()
            st.rerun()

    with card_column:
        with st.expander(f"{index + 1}. {current_title}", expanded=True,
                         key=prefix + "food_expander"):
            direct = item.get("direct_nutrients")
            selected = item.get("selected_food")
            if not direct and meal_locked:
                st.text_input("Αναζήτηση τροφίμου",
                              value=selected.get("description", "") if selected else "",
                              disabled=True, key=prefix + "locked_food_search")
            elif not direct:
                selected_now = st_searchbox(
                    lambda term, key=usda_key, previous=personal_foods,
                    shared=shared_barcode_foods: food_suggestions(
                        term, key, previous, shared
                    ),
                    key=prefix + "live_food_search_v2",
                    label="Αναζήτηση τροφίμου",
                    placeholder="π.χ. ψάρι με πατάτες, ομελέτα ή rice cooked",
                    default=selected,
                    default_searchterm=(selected.get("description", "") if selected else item.get("query", "")),
                    clear_on_submit=False,
                    edit_after_submit="option",
                    style_overrides=searchbox_theme_overrides(),
                    debounce=500,
                )
                if selected_now and selected_now.get("_error"):
                    st.error(selected_now["_error"])
                elif selected_now:
                    previous_selected = item.get("selected_food")
                    previous_selection_id = (
                        item.get("selected_id")
                        or food_selection_identity(previous_selected)
                    )
                    if (selected_now.get("recipe_components")
                            and selected_now.get("source") != "Προσωπική συνταγή"):
                        expanded_recipe = expand_recipe_components(selected_now["recipe_components"])
                        if expanded_recipe:
                            persist_food_editor_state()
                            st.session_state.foods[index:index + 1] = expanded_recipe
                            st.session_state.epoch += 1
                            clear_calculation_state()
                            st.rerun()
                    selected = selected_now
                    item["selected_food"] = selected_now
                    if selected_now.get("source") == "Προσωπική συνταγή":
                        item["stored_recipe_components"] = selected_now.get("recipe_components") or []
                    item["query"] = selected_now.get("description", item.get("query", ""))
                    selection_id = food_selection_identity(selected)
                    if previous_selection_id != selection_id:
                        if not item.get("name"):
                            st.session_state[prefix + "name"] = selected["description"]
                        if selected.get("source") in ("Προσωπικό ιστορικό", "Προσωπική συνταγή"):
                            st.session_state[prefix + "amount"] = float(selected.get("previous_amount") or 100.)
                            st.session_state[prefix + "unit"] = selected.get("previous_unit", "gr")
                        selected_nova = normalize_nova(selected.get("nova_group"))
                        selected_nova_source = str(selected.get("nova_source") or "")
                        item["nova_group"] = selected_nova
                        item["nova_source"] = selected_nova_source
                        # A different search result must never inherit the
                        # previous food's ingredient label or NOVA evidence.
                        for metadata_key in NOVA_METADATA_KEYS:
                            item.pop(metadata_key, None)
                            if metadata_key in selected:
                                item[metadata_key] = selected.get(metadata_key)
                        for suffix in (
                            "nova_guide_ingredients", "nova_guide_additives",
                            "nova_guide_complete", "nova_guide_form",
                            "nova_guide_processing", "nova_guide_industrial",
                            "nova_guide_result", "nova_marker_search",
                        ):
                            st.session_state.pop(prefix + suffix, None)
                        st.session_state[prefix + "nova_group"] = NOVA_OPTIONS[selected_nova or 0]
                        selected_medas = normalize_medas_categories(selected.get("medas_categories"))
                        selected_medas_source = str(selected.get("medas_source") or "")
                        item["medas_categories"] = selected_medas
                        item["medas_source"] = selected_medas_source
                        item["medas_component_amounts"] = normalize_medas_component_amounts(
                            selected.get("medas_component_amounts")
                        )
                        st.session_state[prefix + "medas_categories"] = (
                            medas_options_from_categories(selected_medas)
                            if selected_medas_source == "Επιλογή χρήστη" else []
                        )
                    # Keep the identity in the food row, not only in the
                    # epoch-dependent widget state. This prevents the original
                    # history amount from being reloaded after another row is
                    # deleted and all widget keys are rebuilt.
                    item["selected_id"] = selection_id
                    st.session_state[prefix + "selected_id"] = selection_id

            default_name = item.get("name") or (selected.get("description") if selected else "")
            name_arguments = {} if prefix + "name" in st.session_state else {"value": default_name}
            name = st.text_input("Όνομα στο ημερολόγιο", key=prefix + "name",
                                 on_change="ignore", live="300ms", disabled=meal_locked,
                                 **name_arguments)
            amount_col, unit_col = st.columns([0.72, 0.28])
            with amount_col:
                st.markdown('<span class="mr-quantity-row-marker"></span>', unsafe_allow_html=True)
                amount_arguments = ({} if prefix + "amount" in st.session_state
                                    else {"value": float(item["grams"])})
                amount = st.number_input("Καταναλωθείσα ποσότητα", min_value=0., max_value=10000.,
                                         step=10., key=prefix + "amount", on_change="ignore",
                                         disabled=meal_locked, **amount_arguments)
            with unit_col:
                default_unit = "ml" if item.get("quantity_unit") == "ml" else "gr"
                unit_arguments = ({} if prefix + "unit" in st.session_state
                                  else {"index": 1 if default_unit == "ml" else 0})
                unit_display = st.selectbox("Μονάδα", ["gr", "ml"], key=prefix + "unit",
                                            disabled=meal_locked, **unit_arguments)
            unit = "ml" if unit_display == "ml" else "g"
            basis_unit = item.get("basis_unit", selected.get("basis_unit", "g") if selected else "g")
            density = 1.0
            if unit != basis_unit:
                density_arguments = ({} if prefix + "density" in st.session_state
                                     else {"value": float(item.get("density", 1.0))})
                density = st.number_input("Πυκνότητα (gr/ml)", min_value=0.01, max_value=5.0,
                                          step=0.01, format="%.2f", key=prefix + "density",
                                          help="Χρειάζεται για μετατροπή μεταξύ γραμμαρίων και ml. Χρησιμοποιήστε την ετικέτα ή αξιόπιστη πηγή.",
                                          on_change="ignore", disabled=meal_locked,
                                          **density_arguments)
            # Persist live edits in the row as well as in Streamlit's widget
            # state, so any later key/index change retains the user's values.
            item["name"] = name
            item["grams"] = float(amount)
            item["quantity_unit"] = unit_display
            item["density"] = float(density)
            basis_amount = (amount if unit == basis_unit else
                            amount * density if basis_unit == "g" else amount / density)
            if item.get("note"):
                st.caption(item["note"])
            initial_nova = normalize_nova(
                item.get("nova_group")
                or (selected.get("nova_group") if selected else None)
            )
            initial_nova_source = str(
                item.get("nova_source")
                or (selected.get("nova_source") if selected else "")
            )
            nova_choice = st.selectbox(
                "Κατάταξη επεξεργασίας (NOVA)",
                NOVA_OPTIONS,
                index=initial_nova or 0,
                key=prefix + "nova_group",
                disabled=meal_locked,
                help=NOVA_CHOICE_HELP,
            )
            nova_group = nova_from_option(nova_choice)
            nova_source = (
                initial_nova_source
                if nova_group and nova_group == initial_nova and initial_nova_source
                else "Επιλογή χρήστη" if nova_group else ""
            )
            if nova_group != initial_nova:
                for metadata_key in (
                    "nova_status", "upf_markers", "additive_functions", "nova_reason",
                    "classification_method", "classification_version", "source_reference",
                ):
                    item.pop(metadata_key, None)
                if nova_group:
                    item["nova_status"] = "uncertain"
                    item["nova_reason"] = "Χειροκίνητη επιλογή χρήστη χωρίς τεκμηρίωση από τον οδηγό."
                    item["classification_method"] = "user-selected"
            item["nova_group"] = nova_group
            item["nova_source"] = nova_source
            if nova_group:
                st.caption("Κατάταξη: " + nova_short_label(nova_group)
                           + (" · " + nova_source if nova_source else ""))

            initial_medas = normalize_medas_categories(
                item.get("medas_categories")
                or (selected.get("medas_categories") if selected else None)
            )
            initial_medas_source = str(
                item.get("medas_source")
                or (selected.get("medas_source") if selected else "")
            )
            medas_widget_arguments = ({}
                if prefix + "medas_categories" in st.session_state
                else {"default": (medas_options_from_categories(initial_medas)
                                  if initial_medas_source == "Επιλογή χρήστη" else [])})
            medas_choices = st.multiselect(
                "Κατηγορίες MEDAS (προαιρετική διόρθωση)",
                MEDAS_CATEGORY_OPTIONS,
                key=prefix + "medas_categories",
                disabled=meal_locked,
                placeholder="Κενό = αυτόματη ταξινόμηση",
                help=MEDAS_CHOICE_HELP,
                **medas_widget_arguments,
            )
            if OUTSIDE_CATEGORY_LABEL in medas_choices and len(medas_choices) > 1:
                st.warning(
                    "Η επιλογή «Εκτός άμεσων κριτηρίων MEDAS» δεν συνδυάζεται με άλλη κατηγορία· "
                    "θα χρησιμοποιηθούν οι συγκεκριμένες κατηγορίες που επιλέξατε."
                )
            manual_medas = medas_categories_from_options(medas_choices)
            manual_medas_source = "Επιλογή χρήστη" if manual_medas else ""
            item["medas_categories"] = manual_medas
            item["medas_source"] = manual_medas_source
            existing_medas_amounts = normalize_medas_component_amounts(
                item.get("medas_component_amounts")
                or (selected.get("medas_component_amounts") if selected else None)
            )
            stored_recipe_components = (
                item.get("stored_recipe_components")
                or (selected.get("recipe_components") if selected else None)
                or []
            )
            medas_probe = {
                "Τρόφιμο": name or default_name,
                "Πηγή / εγγραφή": (item.get("database_entry")
                    or (selected.get("details") if selected else "") or ""),
                "Κωδικός βάσης": (
                    "personal-recipe" if selected and selected.get("source") == "Προσωπική συνταγή"
                    else selected.get("source_id", "") if selected else ""
                ),
                "__recipe_components": stored_recipe_components,
                "__medas_categories": manual_medas,
                "Πηγή MEDAS": manual_medas_source,
            }
            effective_medas = classify_food(medas_probe)
            effective_medas_source = medas_classification_source(medas_probe, effective_medas)
            ambiguous_categories = ambiguous_medas_categories(medas_probe, effective_medas)
            medas_component_amounts = {}
            allocation = None
            if ambiguous_categories:
                with st.container(border=True):
                    st.markdown(
                        '<span class="mr-medas-composite-marker"></span>',
                        unsafe_allow_html=True,
                    )
                    component_labels = [
                        medas_component_label(medas_probe, category)
                        for category in ambiguous_categories
                    ]
                    st.error(
                        f"🔴 **{AMBIGUOUS_COMPOSITE_LABEL}**\n\n"
                        f"Αναγνωρίστηκαν: {' και '.join(component_labels)}. "
                        "Δεν είναι γνωστή η ποσότητα κάθε συστατικού."
                    )
                    allocation_unit = "ml" if unit_display == "ml" else "gr"
                    allocation_identity = (
                        (str(selected.get("source") or "") + ":"
                         + str(selected.get("source_id") or "") + ":"
                         + str(selected.get("description") or ""))
                        if selected else
                        (str(item.get("database_entry") or "") + ":" + str(default_name))
                    )
                    allocation_token = hashlib.sha256(
                        allocation_identity.encode("utf-8")
                    ).hexdigest()[:10]
                    for component_index, category in enumerate(ambiguous_categories, start=1):
                        state_key = (prefix + "medas_component_amount_" + category
                                     + "_" + allocation_token)
                        arguments = ({} if state_key in st.session_state else {
                            "value": existing_medas_amounts.get(category)
                        })
                        component_amount = st.number_input(
                            f"{component_index}. {medas_component_label(medas_probe, category)} ({allocation_unit})",
                            min_value=0.0,
                            max_value=10000.0,
                            step=1.0,
                            key=state_key,
                            disabled=meal_locked,
                            **arguments,
                        )
                        if component_amount is not None and component_amount > 0:
                            medas_component_amounts[category] = float(component_amount)
                    allocation_probe = {
                        **medas_probe,
                        "Ποσότητα": amount,
                        "__medas_component_amounts": medas_component_amounts,
                    }
                    allocation = medas_component_allocation(
                        allocation_probe, effective_medas, amount
                    )
                    if allocation["allocated"] > amount + 0.05:
                        st.error(
                            f"Το άθροισμα των συστατικών ({allocation['allocated']:.1f} "
                            f"{allocation_unit}) ξεπερνά τη συνολική ποσότητα "
                            f"({amount:.1f} {allocation_unit})."
                        )
                    elif allocation["valid"]:
                        remainder = max(0.0, amount - allocation["allocated"])
                        st.success(
                            "Η κατανομή MEDAS είναι αξιολογήσιμη."
                            + (f" Υπόλοιπο άλλων συστατικών: {remainder:.1f} {allocation_unit}."
                               if remainder > 0.05 else "")
                        )
                    else:
                        st.warning(AMBIGUOUS_MEDAS_STATUS)
            item["medas_component_amounts"] = medas_component_amounts
            medas_probe["__medas_component_amounts"] = medas_component_amounts
            if ambiguous_categories:
                if allocation and allocation["valid"]:
                    st.caption("MEDAS: κατανομή συστατικών από τον χρήστη")
                else:
                    st.caption("MEDAS: " + AMBIGUOUS_MEDAS_STATUS)
            elif effective_medas:
                st.caption(
                    "MEDAS: " + medas_category_text(effective_medas)
                    + (" · " + effective_medas_source if effective_medas_source else "")
                )
            elif is_known_non_target(medas_probe):
                st.caption(
                    "MEDAS: " + OUTSIDE_CATEGORY_LABEL
                    + (" · " + effective_medas_source if effective_medas_source else "")
                )
            else:
                st.caption(
                    "MEDAS: δεν βρέθηκε αυτόματη κατηγορία. Επιλέξτε μόνο αν γνωρίζετε "
                    "τη σωστή αντιστοίχιση."
                )
            if direct:
                st.caption("Πηγή: " + item.get("database_entry", "Open Food Facts"))
                rows.append((name, amount, {"_direct_nutrients": direct, "_basis_amount": basis_amount,
                                            "_weight_g": amount if unit == "g" else amount * density,
                                            "_entered_unit": unit_display,
                                            "_nova_group": nova_group,
                                            "_nova_source": nova_source,
                                            "_nova_status": item.get("nova_status"),
                                            "_upf_markers": item.get("upf_markers", []),
                                            "_additive_functions": item.get("additive_functions", []),
                                            "_nova_reason": item.get("nova_reason", ""),
                                            "_classification_method": item.get("classification_method", ""),
                                            "_classification_version": item.get("classification_version", ""),
                                            "_source_reference": item.get("source_reference", ""),
                                            "_ingredient_list": item.get("ingredient_list", ""),
                                            "_processing_description": item.get("processing_description", ""),
                                            "_brand": item.get("brand", ""),
                                            "_barcode": item.get("barcode", ""),
                                            "_medas_categories": manual_medas,
                                            "_medas_source": manual_medas_source,
                                            "_medas_component_amounts": medas_component_amounts,
                                            "_recipe_components": item.get("stored_recipe_components", []),
                                            "description": item.get("database_entry", "Open Food Facts")}))
            elif selected:
                selected_basis = selected.get("basis_unit", basis_unit)
                selected_amount = (amount if unit == selected_basis else
                                   amount * density if selected_basis == "g" else amount / density)
                if selected.get("source") in ("Προσωπικό ιστορικό", "Προσωπική συνταγή"):
                    st.caption(f"Παλαιότερη καταχώριση: {selected.get('previous_amount'):g} "
                               f"{selected.get('previous_unit')} · μπορείτε να διορθώσετε ποσότητα και όνομα.")
                rows.append((name or selected["description"], amount,
                             {**selected, "_basis_amount": selected_amount,
                              "_weight_g": amount if unit == "g" else amount * density,
                              "_entered_unit": unit_display,
                              "_nova_group": nova_group,
                              "_nova_source": nova_source,
                              "_nova_status": item.get("nova_status", selected.get("nova_status")),
                              "_upf_markers": item.get("upf_markers", selected.get("upf_markers", [])),
                              "_additive_functions": item.get("additive_functions", selected.get("additive_functions", [])),
                              "_nova_reason": item.get("nova_reason", selected.get("nova_reason", "")),
                              "_classification_method": item.get("classification_method", selected.get("classification_method", "")),
                              "_classification_version": item.get("classification_version", selected.get("classification_version", "")),
                              "_source_reference": item.get("source_reference", selected.get("source_reference", "")),
                              "_ingredient_list": item.get("ingredient_list", selected.get("ingredient_list", "")),
                              "_processing_description": item.get("processing_description", selected.get("processing_description", "")),
                              "_brand": item.get("brand", selected.get("brand", "")),
                              "_barcode": item.get("barcode", selected.get("barcode", "")),
                              "_medas_categories": manual_medas,
                              "_medas_source": manual_medas_source,
                              "_medas_component_amounts": medas_component_amounts,
                              "_recipe_components": (
                                  selected.get("recipe_components", [])
                                  if selected.get("source") == "Προσωπική συνταγή" else []
                              ),
                              "display_description": f"{selected['source']} · {selected['description']}"}))

if st.session_state.foods and not meal_locked:
    st.button("Προσθήκη άλλου τροφίμου", icon=":material/add_circle:",
              on_click=add_search_food, key="add_another_food")

needs_usda = any(not selected.get("_direct_nutrients") for _, _, selected in rows)
if st.button("Υπολογισμός θρεπτικών",
             disabled=meal_locked or not (rows and (usda_key or not needs_usda)), type="primary"):
    try:
        records = []
        with st.spinner("Υπολογισμός θρεπτικών στοιχείων…"):
            for name, amount, selected in rows:
                basis_amount = selected["_basis_amount"]
                direct_nutrients = selected.get("_direct_nutrients")
                detail = None if direct_nutrients else food_detail(selected["fdcId"], usda_key)
                record = {"Τρόφιμο": name, "Ποσότητα": amount,
                    "Μονάδα": selected["_entered_unit"],
                    "Πηγή / εγγραφή": selected.get("display_description", selected["description"]),
                    "Κωδικός βάσης": selected.get("source_id", selected.get("fdcId")),
                    "NOVA": selected.get("_nova_group"),
                    "Πηγή NOVA": selected.get("_nova_source", ""),
                    "__nova_status": selected.get("_nova_status"),
                    "__upf_markers": selected.get("_upf_markers", []),
                    "__additive_functions": selected.get("_additive_functions", []),
                    "__nova_reason": selected.get("_nova_reason", ""),
                    "__classification_method": selected.get("_classification_method", ""),
                    "__classification_version": selected.get("_classification_version", ""),
                    "__source_reference": selected.get("_source_reference", ""),
                    "__ingredient_list": selected.get("_ingredient_list", ""),
                    "__processing_description": selected.get("_processing_description", ""),
                    "__brand": selected.get("_brand", ""),
                    "__barcode": selected.get("_barcode", ""),
                    "__medas_categories": selected.get("_medas_categories", []),
                    "__medas_component_amounts": selected.get(
                        "_medas_component_amounts", {}
                    ),
                    "Πηγή MEDAS": selected.get("_medas_source", ""),
                    "__weight_g": selected["_weight_g"],
                    **(portion_from_per100(direct_nutrients, basis_amount) if direct_nutrients
                       else portion_nutrients(detail, basis_amount))}
                stored_components = selected.get("_recipe_components") or []
                if stored_components:
                    record["__recipe_components"] = scale_recipe_components(
                        stored_components, selected["_weight_g"]
                    )
                effective_categories = classify_food(record)
                record_allocation = medas_component_allocation(
                    record, effective_categories, amount
                )
                if record_allocation["categories"] and not record_allocation["valid"]:
                    record["Κατηγορίες MEDAS"] = AMBIGUOUS_COMPOSITE_LABEL
                    record["Πηγή MEDAS"] = AMBIGUOUS_MEDAS_STATUS
                elif effective_categories:
                    record["Κατηγορίες MEDAS"] = medas_category_text(effective_categories)
                elif is_known_non_target(record):
                    record["Κατηγορίες MEDAS"] = OUTSIDE_CATEGORY_LABEL
                else:
                    record["Κατηγορίες MEDAS"] = "Χωρίς ταξινόμηση"
                if not record.get("Πηγή MEDAS"):
                    record["Πηγή MEDAS"] = (
                        "Κατανομή συστατικών από χρήστη"
                        if record_allocation["valid"]
                        else medas_classification_source(record, effective_categories) or "—"
                    )
                records.append(record)
        frame = pd.DataFrame(records)
        totals = summarize(records, "Σύνολο γεύματος")
        if st.session_state.get("output_signature") != repr(rows) or not st.session_state.get("meal_id"):
            st.session_state.meal_id = str(uuid4())
            st.session_state.meal_saved = False
        st.session_state.output = (frame, totals)
        st.session_state.output_signature = repr(rows)
    except ServiceError as error:
        st.error(str(error))
if (st.session_state.get("output") is not None
        and st.session_state.get("output_signature") == repr(rows)):
    frame, totals = st.session_state.output
    display_frame = ordered_food_frame(frame)
    st.subheader("Αποτελέσματα αυτού του υπολογισμού")
    missing_values = missing_core_nutrients(frame.to_dict(orient="records"))
    if missing_values:
        missing_lines = [
            f"- {CORE_NUTRIENT_WARNINGS[label]}: {', '.join(names)}"
            for label, names in missing_values.items()
        ]
        st.warning(
            "Βρέθηκαν ελλιπή βασικά διατροφικά στοιχεία. Επιλέξτε άλλη εγγραφή "
            "τροφίμου πριν από την αποθήκευση, εφόσον υπάρχει διαθέσιμη:\n\n"
            + "\n".join(missing_lines)
        )
    themed_dataframe(totals, hide_index=True)
    st.caption("Κενό σύνολο σημαίνει ελλιπή δεδομένα. Το άθροισμα διαθέσιμων δεν είναι πλήρες σύνολο γεύματος. Αλλάξατε στοιχεία; Πατήστε ξανά Υπολογισμός.")
    themed_dataframe(display_frame, hide_index=True)
    if diary:
        recipe_indices = []
        recipe_title = ""
        recipe_disabled = False
        recipe_final_weight = None
        recipe_servings = 1.0
        recipe_method = "Δεν δηλώθηκε"
        recipe_processing = []
        recipe_yield_source = "measured"
        recipe_yield_reference = ""
        if len(frame.index) >= 2:
            recipe_open_key = f"recipe_builder_open_{st.session_state.meal_id}"
            if recipe_edit_context:
                st.session_state[recipe_open_key] = True
                st.subheader(
                    "Αποθήκευση αντιγράφου" if recipe_edit_context.get("mode") == "copy"
                    else "Ενημέρωση συνταγής"
                )
            else:
                if st.button(
                    "Αποθήκευση των παραπάνω ως μίξη ή συνταγή",
                    icon=":material/menu_book:", width="stretch", wrap=True,
                    type=("primary" if st.session_state.get(recipe_open_key) else "secondary"),
                    key=f"toggle_{recipe_open_key}",
                ):
                    st.session_state[recipe_open_key] = not st.session_state.get(
                        recipe_open_key, False
                    )
                    st.rerun()
            if st.session_state.get(recipe_open_key, False):
                if recipe_edit_context:
                    recipe_indices = list(range(len(frame.index)))
                    st.caption(
                        "Όλα τα παραπάνω τρόφιμα θα αποθηκευτούν ως υλικά της "
                        "συνταγής. Χρησιμοποιήστε τα πλαίσια τροφίμων πιο πάνω "
                        "για αλλαγή ποσότητας ή διαγραφή υλικού."
                    )
                else:
                    st.caption(
                        "Επιλέξτε τουλάχιστον δύο τρόφιμα. Τα ίδια υλικά θα "
                        "ομαδοποιηθούν στο ημερολόγιο και μπορούν να αποθηκευτούν "
                        "ως προσωπική μίξη ή συνταγή."
                    )
                    recipe_columns = st.columns(2)
                    for index, food_name in enumerate(frame["Τρόφιμο"].astype(str)):
                        with recipe_columns[index % 2]:
                            if st.checkbox(
                                f"{index + 1}. {food_name}",
                                key=f"recipe_{st.session_state.meal_id}_{index}",
                            ):
                                recipe_indices.append(index)
                if len(recipe_indices) >= 2:
                    recipe_title = st.text_input(
                        "Δώστε τίτλο μίξης ή συνταγής",
                        value=(recipe_edit_context.get("draft_title", "")
                               if recipe_edit_context else ""),
                        key=f"recipe_title_{st.session_state.meal_id}",
                    )
                    selected_records = [
                        frame.to_dict(orient="records")[index]
                        for index in recipe_indices
                    ]
                    raw_weight = sum(
                        float(record.get("__weight_g", record.get("Ποσότητα", 0)) or 0)
                        for record in selected_records
                    )
                    recipe_methods = [
                        "Απλή μίξη υλικών", "Χωρίς μαγείρεμα",
                        "Ψήσιμο στον φούρνο", "Βράσιμο",
                        "Μαγείρεμα στον ατμό", "Τηγάνισμα",
                        "Μαγείρεμα στην κατσαρόλα", "Άλλο",
                    ]
                    default_recipe_method = (
                        recipe_edit_context.get("cooking_method")
                        if recipe_edit_context else "Απλή μίξη υλικών"
                    )
                    if default_recipe_method not in recipe_methods:
                        default_recipe_method = "Άλλο"
                    recipe_method = st.selectbox(
                        "Τρόπος παρασκευής",
                        recipe_methods,
                        index=recipe_methods.index(default_recipe_method),
                        key=f"recipe_method_{st.session_state.meal_id}",
                        help=("Η απλή μίξη αποθηκεύει τα επιλεγμένα υλικά χωρίς "
                              "μεταβολή βάρους ή Retention factors. Οι πρόσθετες "
                              "επιλογές εμφανίζονται μόνο για συγκεκριμένη "
                              "θερμική επεξεργασία."),
                    )
                    advanced_cooking = recipe_method in {
                        "Ψήσιμο στον φούρνο", "Βράσιμο",
                        "Μαγείρεμα στον ατμό", "Τηγάνισμα",
                        "Μαγείρεμα στην κατσαρόλα",
                    }

                    if advanced_cooking:
                        st.markdown("**Αντιστοίχιση υλικών με τις κατηγορίες EuroFIR**")
                        st.caption(
                            "Ελέγξτε την προτεινόμενη κατηγορία κάθε υλικού. Αν το "
                            "τρόφιμο της βάσης είναι ήδη μαγειρεμένο, δεν εφαρμόζεται "
                            "δεύτερη διόρθωση."
                        )
                        category_labels = list(CATEGORY_OPTIONS)
                        for ingredient_index, record in enumerate(selected_records):
                            food_name = str(
                                record.get("Τρόφιμο") or f"Υλικό {ingredient_index + 1}"
                            )
                            inferred_category = infer_category(food_name)
                            category_column, cooked_column = st.columns([3, 2])
                            with category_column:
                                selected_category_label = st.selectbox(
                                    food_name,
                                    category_labels,
                                    index=category_index(inferred_category),
                                    key=(f"recipe_eurofir_category_"
                                         f"{st.session_state.meal_id}_{ingredient_index}"),
                                )
                            with cooked_column:
                                already_cooked = st.checkbox(
                                    "Ήδη μαγειρεμένο",
                                    value=looks_already_cooked(
                                        food_name, record.get("Πηγή / εγγραφή")
                                    ),
                                    key=(f"recipe_eurofir_cooked_"
                                         f"{st.session_state.meal_id}_{ingredient_index}"),
                                    help=("Ενεργοποιήστε το όταν η εγγραφή βάσης είναι "
                                          "ήδη βραστή, ψητή ή τηγανητή."),
                                )
                            recipe_processing.append({
                                "category": CATEGORY_OPTIONS[selected_category_label],
                                "already_cooked": already_cooked,
                            })

                        yield_modes = [
                            "Το ζύγισα μετά την παρασκευή",
                            "Εκτίμηση από αντίστοιχο ολόκληρο φαγητό Bognár",
                            COMPONENT_YIELD_MODE,
                        ]
                        if recipe_edit_context:
                            yield_modes.insert(
                                0, "Διατήρηση αποθηκευμένου τελικού βάρους"
                            )
                        yield_mode = st.radio(
                            "Τελικό βάρος συνταγής",
                            yield_modes,
                            key=f"recipe_yield_mode_{st.session_state.meal_id}",
                            help=("Η πραγματική ζύγιση είναι ακριβέστερη. Η εκτίμηση "
                                  "ολόκληρου φαγητού είναι η επόμενη προτιμώμενη "
                                  "λύση. Η εκτίμηση ανά υλικό είναι εφεδρική και "
                                  "χαμηλότερης βεβαιότητας."),
                        )
                        servings_column, weight_column = st.columns(2)
                        with servings_column:
                            recipe_servings = st.number_input(
                                "Αριθμός μερίδων ή κομματιών",
                                min_value=1.0, max_value=1000.0,
                                value=max(1.0, float(
                                    recipe_edit_context.get("servings", 1.0)
                                    if recipe_edit_context else 1.0
                                )), step=1.0,
                                key=f"recipe_servings_{st.session_state.meal_id}",
                            )
                        with weight_column:
                            if yield_mode == "Διατήρηση αποθηκευμένου τελικού βάρους":
                                recipe_final_weight = float(
                                    recipe_edit_context.get("final_weight_g") or raw_weight
                                )
                                recipe_yield_source = (
                                    recipe_edit_context.get("yield_source")
                                    or "Αποθηκευμένο τελικό βάρος"
                                )
                                recipe_yield_reference = (
                                    recipe_edit_context.get("yield_reference")
                                    or "Διατήρηση από την προηγούμενη αποθήκευση"
                                )
                                st.metric(
                                    "Αποθηκευμένο τελικό βάρος",
                                    f"{recipe_final_weight:.1f} gr",
                                )
                            elif yield_mode == "Το ζύγισα μετά την παρασκευή":
                                recipe_yield_source = "Ζυγισμένο τελικό βάρος"
                                recipe_yield_reference = "Πραγματική μέτρηση χρήστη"
                                recipe_final_weight = st.number_input(
                                    "Τελικό ζυγισμένο βάρος (gr)",
                                    min_value=1.0, max_value=100000.0,
                                    value=max(1.0, float(
                                        recipe_edit_context.get("final_weight_g", raw_weight)
                                        if recipe_edit_context else raw_weight
                                    )), step=10.0,
                                    key=f"recipe_final_weight_{st.session_state.meal_id}",
                                )
                            elif yield_mode == "Εκτίμηση από αντίστοιχο ολόκληρο φαγητό Bognár":
                                recipe_categories = [
                                    item["category"] for item in recipe_processing
                                ]
                                mixed_recipe = len(selected_records) >= 2
                                st.info(
                                    "**Μικτή συνταγή:** εμφανίζονται μόνο δημοσιευμένα "
                                    "προφίλ Bognár για φαγητά με συστατικά. Ένας "
                                    "συντελεστής μεμονωμένου τροφίμου δεν εφαρμόζεται "
                                    "πλέον στο συνολικό βάρος της συνταγής."
                                )
                                show_all_bognar = st.checkbox(
                                    "Εμφάνιση όλων των συμβατών προφίλ Bognár",
                                    value=False,
                                    key=(f"recipe_yield_all_profiles_"
                                         f"{st.session_state.meal_id}"),
                                    help=("Κανονικά εμφανίζονται πρώτα τα προφίλ που "
                                          "ταιριάζουν στις κατηγορίες των υλικών. Η "
                                          "πλήρης λίστα προορίζεται για έλεγχο από "
                                          "χρήστη με γνώση της συνταγής."),
                                )
                                profiles = yield_profiles_for(
                                    recipe_method,
                                    ingredient_categories=recipe_categories,
                                    mixed_recipe=mixed_recipe,
                                    include_all=show_all_bognar,
                                )
                                profile_label = st.selectbox(
                                    "Προφίλ εκτίμησης βάρους",
                                    list(profiles),
                                    key=f"recipe_yield_profile_{st.session_state.meal_id}",
                                )
                                available_profile_count = sum(
                                    profile is not None for profile in profiles.values()
                                )
                                st.caption(
                                    f"{available_profile_count} δημοσιευμένα προφίλ "
                                    "είναι συμβατά με τον τρόπο παρασκευής και το "
                                    "τρέχον φίλτρο υλικών."
                                )
                                profile = profiles.get(profile_label)
                                if profile:
                                    st.caption(
                                        yield_profile_documentation(
                                            profile, recipe_categories
                                        )
                                    )
                                recipe_final_weight = estimate_final_weight(
                                    raw_weight, profile_label
                                )
                                recipe_yield_source = "Εκτίμηση πίνακα"
                                recipe_yield_reference = (
                                    f"{profile_label} · {profile.get('reference', '')} · "
                                    f"ID {profile.get('id', '')}"
                                    if profile else "Δεν επιλέχθηκε κατάλληλο προφίλ"
                                )
                                if recipe_final_weight is None:
                                    st.error(
                                        "Δεν υπάρχει ασφαλής εκτίμηση για αυτή την επιλογή. "
                                        "Επιλέξτε πραγματική ζύγιση ή κατάλληλο προφίλ."
                                    )
                                else:
                                    st.metric(
                                        "Εκτιμώμενο τελικό βάρος",
                                        f"{recipe_final_weight:.1f} gr",
                                    )
                            else:
                                warning_label, warning_help = st.columns(
                                    [9, 1], gap="small"
                                )
                                with warning_label:
                                    st.caption(
                                        "**Προσεγγιστική εκτίμηση ανά υλικό — "
                                        "χαμηλότερη βεβαιότητα**"
                                    )
                                with warning_help:
                                    show_component_warning = st.button(
                                        "?",
                                        key=(f"recipe_component_yield_warning_"
                                             f"{st.session_state.meal_id}"),
                                        help=COMPONENT_YIELD_WARNING,
                                        type="tertiary",
                                    )
                                if show_component_warning:
                                    st.warning(COMPONENT_YIELD_WARNING)
                                component_rows = []
                                component_references = []
                                component_total = 0.0
                                missing_component_profile = False
                                for ingredient_index, record in enumerate(selected_records):
                                    food_name = str(
                                        record.get("Τρόφιμο")
                                        or f"Υλικό {ingredient_index + 1}"
                                    )
                                    ingredient_weight = float(
                                        record.get(
                                            "__weight_g", record.get("Ποσότητα", 0)
                                        ) or 0
                                    )
                                    processing = recipe_processing[ingredient_index]
                                    if processing["already_cooked"]:
                                        component_final = ingredient_weight
                                        component_total += component_final
                                        component_rows.append({
                                            "Υλικό": food_name,
                                            "Αρχικό (gr)": ingredient_weight,
                                            "Yield factor": 1.0,
                                            "Τελικό (gr)": component_final,
                                            "Προφίλ": "Ήδη μαγειρεμένο — χωρίς νέα μεταβολή",
                                        })
                                        component_references.append(
                                            f"{food_name}: ήδη μαγειρεμένο, factor 1.00"
                                        )
                                        continue

                                    ingredient_profiles = ingredient_yield_profiles_for(
                                        recipe_method,
                                        processing["category"],
                                        food_name,
                                    )
                                    ingredient_labels = list(ingredient_profiles)
                                    ingredient_label = st.selectbox(
                                        f"Προφίλ βάρους — {food_name}",
                                        ingredient_labels,
                                        index=(1 if len(ingredient_labels) > 1 else 0),
                                        key=(f"recipe_component_yield_"
                                             f"{st.session_state.meal_id}_{ingredient_index}"),
                                    )
                                    ingredient_profile = ingredient_profiles.get(
                                        ingredient_label
                                    )
                                    if not ingredient_profile:
                                        missing_component_profile = True
                                        component_rows.append({
                                            "Υλικό": food_name,
                                            "Αρχικό (gr)": ingredient_weight,
                                            "Yield factor": "—",
                                            "Τελικό (gr)": "Μη εκτιμήσιμο",
                                            "Προφίλ": "Δεν επιλέχθηκε",
                                        })
                                        continue
                                    component_factor = float(
                                        ingredient_profile["factor"]
                                    )
                                    component_final = ingredient_weight * component_factor
                                    component_total += component_final
                                    component_rows.append({
                                        "Υλικό": food_name,
                                        "Αρχικό (gr)": ingredient_weight,
                                        "Yield factor": component_factor,
                                        "Τελικό (gr)": component_final,
                                        "Προφίλ": ingredient_profile.get(
                                            "name_original", ingredient_label
                                        ),
                                    })
                                    component_references.append(
                                        f"{food_name}: {ingredient_label} · "
                                        f"ID {ingredient_profile.get('id', '')}"
                                    )
                                    st.caption(
                                        yield_profile_documentation(
                                            ingredient_profile,
                                            [processing["category"]],
                                        )
                                    )

                                themed_dataframe(
                                    pd.DataFrame(component_rows), hide_index=True
                                )
                                if missing_component_profile:
                                    recipe_final_weight = None
                                    st.error(
                                        "Δεν έχει επιλεγεί κατάλληλο δημοσιευμένο "
                                        "προφίλ για όλα τα άψητα υλικά. Επιλέξτε "
                                        "προφίλ ανά υλικό ή χρησιμοποιήστε πραγματική ζύγιση."
                                    )
                                else:
                                    recipe_final_weight = component_total
                                    recipe_yield_source = (
                                        "Προσεγγιστική εκτίμηση ανά υλικό Bognár "
                                        "— χαμηλότερη βεβαιότητα"
                                    )
                                    recipe_yield_reference = (
                                        "Εφεδρική προσέγγιση, όχι η προτεινόμενη "
                                        "εναρμονισμένη μέθοδος EuroFIR για μικτή "
                                        "συνταγή | " + " | ".join(
                                            component_references
                                        )
                                    )
                                    st.metric(
                                        "Εκτιμώμενο τελικό βάρος",
                                        f"{recipe_final_weight:.1f} gr",
                                    )
                        if recipe_final_weight is not None:
                            yield_factor = float(recipe_final_weight) / max(float(raw_weight), 1.0)
                            weight_change = (yield_factor - 1.0) * 100.0
                            if abs(weight_change) < 0.05:
                                change_text = "καμία ουσιαστική μεταβολή βάρους"
                            elif weight_change < 0:
                                change_text = f"απώλεια βάρους {abs(weight_change):.1f}%"
                            else:
                                change_text = f"αύξηση βάρους {weight_change:.1f}%"
                            st.info(
                                f"**Διατήρηση βάρους:** {yield_factor * 100:.1f}% — "
                                f"{change_text}. Αρχικό {float(raw_weight):.1f} gr, "
                                f"τελικό {float(recipe_final_weight):.1f} gr."
                            )

                        if method_group(recipe_method) == "fat":
                            st.warning(
                                "Οι δημοσιευμένοι συνοπτικοί πίνακες EuroFIR για "
                                "μαγείρεμα με λίπος δεν είναι ολοκληρωμένοι. Δεν "
                                "εφαρμόζονται μη τεκμηριωμένοι Retention factors ούτε "
                                "εκτιμάται αυτόματα η πρόσληψη λαδιού. Καταχωρίστε το "
                                "λάδι ως ξεχωριστό υλικό."
                            )
                    else:
                        recipe_final_weight = float(raw_weight)
                        recipe_servings = st.number_input(
                            "Αριθμός μερίδων ή κομματιών",
                            min_value=1.0, max_value=1000.0,
                            value=max(1.0, float(
                                recipe_edit_context.get("servings", 1.0)
                                if recipe_edit_context else 1.0
                            )), step=1.0,
                            key=f"recipe_servings_{st.session_state.meal_id}",
                        )
                        recipe_processing = [
                            {
                                "category": infer_category(record.get("Τρόφιμο")),
                                "already_cooked": False,
                            }
                            for record in selected_records
                        ]
                        if recipe_method == "Απλή μίξη υλικών":
                            recipe_yield_source = "Απλή μίξη υλικών"
                            recipe_yield_reference = (
                                "Χωρίς μαγείρεμα — άθροισμα βαρών υλικών"
                            )
                            st.caption(
                                "Η μίξη θα αποθηκευτεί για επαναχρησιμοποίηση. "
                                "Δεν εφαρμόζεται μεταβολή βάρους ή Retention factor."
                            )
                        elif recipe_method == "Χωρίς μαγείρεμα":
                            recipe_yield_source = "Χωρίς μαγείρεμα"
                            recipe_yield_reference = (
                                "Άθροισμα βαρών υλικών — Retention factor 1,00"
                            )
                            st.caption(
                                "Το τελικό βάρος ισούται με το άθροισμα των "
                                "υλικών και δεν εφαρμόζονται απώλειες μαγειρέματος."
                            )
                        else:
                            recipe_yield_source = (
                                "Άλλη μέθοδος — χωρίς υπολογισμό μαγειρέματος"
                            )
                            recipe_yield_reference = (
                                "Δεν υπάρχει δηλωμένη τεκμηριωμένη μέθοδος"
                            )
                            st.warning(
                                "Για την επιλογή «Άλλο» δεν εφαρμόζονται Yield "
                                "ή Retention factors. Η συνταγή αποθηκεύεται ως "
                                "απλή μίξη με άθροισμα των αρχικών βαρών."
                            )

                    selected_missing = missing_core_nutrients(selected_records)
                    if selected_missing:
                        st.warning(
                            "Η συνταγή έχει ελλιπή βασικά διατροφικά στοιχεία. "
                            "Αν υπάρχει πληρέστερη εγγραφή βάσης, αντικαταστήστε "
                            "το αντίστοιχο υλικό."
                        )
                    save_recipe_disabled = bool(
                        selected_missing or not recipe_title.strip()
                        or recipe_final_weight is None
                    )
                    save_recipe_label = "Αποθήκευση μίξης / συνταγής"
                    if recipe_edit_context:
                        save_recipe_label = (
                            "Αποθήκευση αντιγράφου"
                            if recipe_edit_context.get("mode") == "copy"
                            else "Αποθήκευση αλλαγών"
                        )
                    if st.button(
                        save_recipe_label,
                        icon=":material/bookmark_add:", type="secondary",
                        disabled=save_recipe_disabled,
                        key=f"save_recipe_{st.session_state.meal_id}",
                    ):
                        try:
                            stored_title = diary.save_personal_recipe(
                                recipe_title, selected_records, recipe_final_weight,
                                recipe_servings, recipe_method,
                                recipe_processing, recipe_yield_source,
                                recipe_yield_reference,
                                recipe_id=(
                                    recipe_edit_context.get("recipe_id")
                                    if recipe_edit_context
                                    and recipe_edit_context.get("mode") == "replace"
                                    else None
                                ),
                                create_copy=bool(
                                    recipe_edit_context
                                    and recipe_edit_context.get("mode") == "copy"
                                ),
                            )
                        except DiaryError as error:
                            st.error(str(error))
                        else:
                            st.session_state.personal_foods_dirty = True
                            if recipe_edit_context:
                                st.session_state.recipe_saved_notice = (
                                    f"Η συνταγή «{stored_title}» αποθηκεύτηκε και "
                                    "είναι διαθέσιμη στην αναζήτηση τροφίμων."
                                )
                                reset_meal()
                                st.switch_page(recipe_page)
                            else:
                                st.success(
                                    f"Η μίξη/συνταγή «{stored_title}» αποθηκεύτηκε και "
                                    "είναι διαθέσιμη στην αναζήτηση τροφίμων."
                                )
            recipe_disabled = len(recipe_indices) >= 2 and not recipe_title.strip()
        if recipe_edit_context:
            st.caption(
                "Η επεξεργασία αποθηκεύει τη συνταγή και δεν δημιουργεί νέα "
                "καταχώριση στο ημερολόγιο."
            )
        else:
            effective_recipe_indices = [] if recipe_disabled else recipe_indices
            records_to_save = combine_food_records(
                frame.to_dict(orient="records"), effective_recipe_indices, recipe_title)
            save_frame = pd.DataFrame(records_to_save)
            saved_day = save_meal(diary, save_frame, disabled=recipe_disabled)
            if saved_day:
                reset_meal()
                st.session_state.meal_saved_notice = saved_day
                st.rerun()
    download_menu(
        "Λήψη αποτελεσμάτων γεύματος",
        display_frame,
        "meal",
        excel_sheets=[
            ("Τρόφιμα", display_frame),
            ("Σύνολα και κάλυψη", totals),
        ],
        key=f"meal_download_{st.session_state.meal_id}",
    )
st.caption("Πηγές: USDA FoodData Central, CoFID 2021, δοκιμαστικό υποσύνολο τυριών από τους Πίνακες Σύνθεσης Ελληνικών Τροφίμων και Open Food Facts για στοιχεία barcode/NOVA. Εκπαιδευτική εκτίμηση. Στο ημερολόγιο διατηρούνται μόνο γεύματα για τα οποία πατήσατε Αποθήκευση. Οι φωτογραφίες δεν αποθηκεύονται στο ημερολόγιο.")
