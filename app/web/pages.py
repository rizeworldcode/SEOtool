"""
Footer Inside Pages Generator for RizeMetrics / LinkPilot Web UI.
Serves dedicated standalone pages for all footer resources, about, research, and legal links
with identical theme styling (curved lime hero, floating capsule header, dark bottom bar).
"""

from typing import Dict, Any, Optional
import html
import os
import base64

_LOGO_FILE = os.path.join(os.path.dirname(__file__), "logo.png")
_LOGO_SRC = "/logo.png"
if os.path.exists(_LOGO_FILE):
    try:
        with open(_LOGO_FILE, "rb") as _f:
            _LOGO_SRC = f"data:image/png;base64,{base64.b64encode(_f.read()).decode('ascii')}"
    except Exception:
        _LOGO_SRC = "/logo.png"


def _get_page_styles() -> str:
    """Return common CSS styles matching the main RizeMetrics dashboard."""
    return """
    :root {
      --bg-canvas: #f1f5f9;
      --bg-card: #ffffff;
      --bg-subtle: #f8fafc;
      --border-light: #cbd5e1;
      --border-subtle: #e2e8f0;
      
      --text-main: #0f172a;
      --text-muted: #334155;
      --text-soft: #64748b;
      --text-light: #94a3b8;
      
      --accent-lime: #d9f942;
      --accent-lime-hover: #c8ea2e;
      --nav-dark: #171b26;
      
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      
      --shadow-card: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.03);
      --shadow-hover: 0 12px 20px -4px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.04);
      --radius-xl: 20px;
      --radius-2xl: 24px;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }

    html, body {
      max-width: 100%;
      overflow-x: hidden;
      position: relative;
    }

    body {
      background-color: var(--bg-canvas);
      color: var(--text-main);
      min-height: 100vh;
      line-height: 1.6;
      padding-bottom: 0;
    }

    h1, h2, h3, h4, .brand-title, .page-main-title, .hero-main-title {
      font-family: 'Outfit', sans-serif;
    }

    .mono {
      font-family: 'JetBrains Mono', monospace;
    }

    /* Top Floating Capsule Navigation */
    .nav-container {
      width: 96%;
      max-width: 1860px;
      margin: 0 auto;
      padding: 1.25rem 1.5rem 0.6rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }

    @media (max-width: 768px) {
      .nav-container {
        width: 100%;
        padding: 0.75rem 1rem 0.5rem;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 0.75rem;
      }
      .brand-group {
        gap: 0.65rem;
      }
      .brand-logo-img {
        width: 36px;
        height: 36px;
      }
      .brand-title {
        font-size: 1.15rem;
      }
      .brand-subtitle {
        display: none;
      }
      .nav-actions {
        margin-left: auto;
      }
      .quick-export-btn {
        padding: 0.38rem 0.85rem;
        font-size: 0.78rem;
      }
      .capsule-navbar {
        width: 100%;
        order: 3;
        justify-content: center;
        padding: 0.25rem 0.35rem;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        border-radius: 9999px;
      }
      .capsule-item {
        padding: 0.38rem 0.75rem;
        font-size: 0.76rem;
        white-space: nowrap;
      }
    }

    .brand-group {
      display: flex;
      align-items: center;
      gap: 0.85rem;
      text-decoration: none;
    }

    .brand-logo-img {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: #ffffff;
      padding: 3px;
      object-fit: contain;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
      border: 1px solid var(--border-light);
    }

    .brand-title {
      font-weight: 800;
      font-size: 1.25rem;
      letter-spacing: -0.02em;
      line-height: 1.15;
    }

    .brand-rize {
      color: #ee591e;
    }

    .brand-metrics {
      color: #2a7c37;
    }

    .brand-subtitle {
      font-size: 0.72rem;
      color: var(--text-soft);
      font-weight: 500;
      display: block;
    }

    .capsule-navbar {
      display: flex;
      align-items: center;
      background: var(--nav-dark);
      border-radius: 9999px;
      padding: 0.35rem 0.45rem;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.15);
      border: 1px solid #282f3f;
    }

    .capsule-item {
      padding: 0.45rem 1rem;
      font-size: 0.8rem;
      font-weight: 600;
      color: #94a3b8;
      border-radius: 9999px;
      text-decoration: none;
      transition: all 0.15s ease;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.45rem;
      border: none;
      background: transparent;
    }

    .capsule-item:hover {
      color: #ffffff;
    }

    .capsule-item.active {
      background: var(--accent-lime);
      color: #0f172a;
      font-weight: 700;
      box-shadow: 0 2px 8px rgba(217, 249, 66, 0.35);
    }

    .nav-actions {
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }

    .quick-export-btn {
      padding: 0.55rem 1.15rem;
      font-size: 0.82rem;
      font-weight: 700;
      border-radius: 12px;
      background: #ffffff;
      border: 1px solid var(--border-light);
      color: #0f172a;
      display: flex;
      align-items: center;
      gap: 0.45rem;
      cursor: pointer;
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      transition: all 0.15s;
      text-decoration: none;
    }

    .quick-export-btn:hover {
      background: #f8fafc;
      border-color: #94a3b8;
    }

    /* Curved Hero Section matching Search Bar Theme */
    .curved-hero-section {
      width: 100%;
      background: var(--accent-lime);
      background-image: radial-gradient(circle at 50% 20%, rgba(255, 255, 255, 0.42) 0%, rgba(217, 249, 66, 0) 70%),
                        linear-gradient(180deg, #dcfc40 0%, #cbee26 100%);
      padding: 3.5rem 1.5rem 4.5rem;
      position: relative;
      text-align: center;
      border-bottom-left-radius: 50% 55px;
      border-bottom-right-radius: 50% 55px;
      box-shadow: 0 12px 36px rgba(217, 249, 66, 0.32);
      margin-bottom: 2.5rem;
      overflow: hidden;
      box-sizing: border-box;
    }

    .hero-inner-container {
      max-width: 860px;
      width: 100%;
      margin: 0 auto;
      position: relative;
    }

    .hero-badge-category {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      background: var(--nav-dark);
      color: #ffffff;
      font-size: 0.72rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      padding: 0.3rem 0.85rem;
      border-radius: 9999px;
      margin-bottom: 1.15rem;
      box-shadow: 0 2px 8px rgba(0,0,0,0.12);
    }

    .hero-badge-category .dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent-lime);
      display: inline-block;
    }

    .hero-main-title {
      font-size: clamp(2rem, 4.5vw, 3.2rem);
      font-weight: 900;
      color: #0f172a;
      letter-spacing: -0.03em;
      line-height: 1.15;
      margin-bottom: 0.95rem;
    }

    .hero-desc {
      font-size: clamp(0.95rem, 2vw, 1.12rem);
      color: #1e293b;
      line-height: 1.6;
      max-width: 720px;
      margin: 0 auto 1.8rem;
      font-weight: 500;
    }

    .hero-quick-action {
      display: inline-flex;
      align-items: center;
      gap: 0.75rem;
      background: #ffffff;
      border: 1px solid rgba(255, 255, 255, 0.9);
      border-radius: 9999px;
      padding: 0.45rem 0.5rem 0.45rem 1.4rem;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.12);
    }

    .hero-quick-action input {
      border: none;
      outline: none;
      font-size: 0.92rem;
      width: 240px;
      color: var(--text-main);
    }

    .hero-quick-action button {
      background: var(--nav-dark);
      color: #ffffff;
      border: none;
      outline: none;
      padding: 0.65rem 1.4rem;
      border-radius: 9999px;
      font-size: 0.84rem;
      font-weight: 700;
      cursor: pointer;
      transition: background 0.15s;
    }

    .hero-quick-action button:hover {
      background: #000000;
    }

    /* Page Content Layout */
    .container {
      width: 96%;
      max-width: 1400px;
      margin: 0 auto;
      padding: 0 1.5rem 4rem;
      box-sizing: border-box;
    }

    .content-card {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-2xl);
      box-shadow: var(--shadow-card);
      padding: 3rem 3.5rem;
      margin-bottom: 2.5rem;
    }

    .content-section {
      margin-bottom: 2.75rem;
    }

    .content-section:last-child {
      margin-bottom: 0;
    }

    .section-title {
      font-size: 1.65rem;
      font-weight: 800;
      color: var(--text-main);
      margin-bottom: 1rem;
      letter-spacing: -0.02em;
      display: flex;
      align-items: center;
      gap: 0.65rem;
    }

    .section-title-icon {
      color: var(--primary);
      display: inline-flex;
    }

    .prose-text {
      color: var(--text-muted);
      font-size: 1rem;
      line-height: 1.75;
      margin-bottom: 1.25rem;
    }

    .feature-grid-3 {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1.5rem;
      margin: 1.75rem 0;
    }

    .feature-grid-2 {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 1.5rem;
      margin: 1.75rem 0;
    }

    .feature-box {
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 1.6rem 1.8rem;
      transition: all 0.2s ease;
    }

    .feature-box:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 18px rgba(0,0,0,0.04);
      border-color: var(--border-light);
    }

    .feature-box-title {
      font-size: 1.15rem;
      font-weight: 800;
      color: var(--text-main);
      margin-bottom: 0.6rem;
      font-family: 'Outfit', sans-serif;
    }

    .feature-box-desc {
      font-size: 0.88rem;
      color: var(--text-soft);
      line-height: 1.55;
    }

    .data-table-wrap {
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      overflow-x: auto;
      margin: 1.5rem 0;
    }

    .page-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
      text-align: left;
    }

    .page-table th {
      background: #f8fafc;
      padding: 0.85rem 1.2rem;
      font-weight: 700;
      color: var(--text-main);
      border-bottom: 1px solid var(--border-subtle);
      font-family: 'Outfit', sans-serif;
    }

    .page-table td {
      padding: 0.85rem 1.2rem;
      border-bottom: 1px solid var(--border-subtle);
      color: var(--text-muted);
    }

    .page-table tr:last-child td {
      border-bottom: none;
    }

    .tag-badge {
      display: inline-block;
      padding: 0.25rem 0.65rem;
      border-radius: 6px;
      font-size: 0.76rem;
      font-weight: 700;
    }

    .tag-green { background: #ecfdf5; color: #059669; border: 1px solid #a7f3d0; }
    .tag-blue { background: #eff6ff; color: #2563eb; border: 1px solid #bfdbfe; }
    .tag-amber { background: #fffbeb; color: #d97706; border: 1px solid #fde68a; }

    .code-snippet-box {
      background: var(--nav-dark);
      color: #e2e8f0;
      padding: 1.25rem 1.5rem;
      border-radius: 14px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.84rem;
      line-height: 1.6;
      overflow-x: auto;
      margin: 1.5rem 0;
    }

    /* Footer Styling matching Lime Theme */
    .site-footer {
      width: 100%;
      background: transparent;
      margin-top: 4rem;
      position: relative;
      box-sizing: border-box;
    }

    .footer-main-wrap {
      width: 100%;
      background: var(--accent-lime);
      background-image: radial-gradient(circle at 50% 15%, rgba(255, 255, 255, 0.45) 0%, rgba(217, 249, 66, 0) 70%),
                        linear-gradient(180deg, #dcfc40 0%, #cbee26 100%);
      border-top: 1px solid rgba(15, 23, 42, 0.08);
      border-top-left-radius: 50% 45px;
      border-top-right-radius: 50% 45px;
      padding: 3.5rem 1.5rem 4rem;
      box-shadow: 0 -12px 36px rgba(217, 249, 66, 0.28);
      box-sizing: border-box;
      overflow: hidden;
    }

    .footer-cta-wrap {
      width: 96%;
      max-width: 1860px;
      margin: 0 auto 3rem;
      padding-bottom: 2.75rem;
      border-bottom: 1px solid rgba(15, 23, 42, 0.15);
      box-sizing: border-box;
    }

    .footer-cta-card {
      background: #ffffff;
      border: 1px solid rgba(255, 255, 255, 0.9);
      border-radius: 9999px;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
      padding: 0.95rem 1.25rem 0.95rem 2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1.25rem;
      flex-wrap: nowrap;
      width: 100%;
      box-sizing: border-box;
    }

    .footer-cta-left {
      display: flex;
      align-items: center;
      gap: 1rem;
      min-width: 0;
      flex: 1 1 auto;
      flex-wrap: nowrap;
    }

    .footer-cta-title {
      font-size: 1.35rem;
      font-weight: 800;
      color: #0f172a;
      letter-spacing: -0.02em;
      margin: 0;
      font-family: 'Outfit', sans-serif;
      white-space: nowrap;
      flex-shrink: 0;
    }

    .footer-cta-subtitle {
      font-size: 0.9rem;
      color: #334155;
      font-weight: 500;
      line-height: 1.35;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }

    .footer-cta-right {
      flex-shrink: 0;
    }

    .footer-cta-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: var(--nav-dark);
      color: #ffffff;
      font-weight: 700;
      font-size: 0.9rem;
      padding: 0.75rem 1.75rem;
      border-radius: 9999px;
      text-decoration: none;
      box-shadow: 0 4px 14px rgba(23, 27, 38, 0.35);
      transition: all 0.2s ease;
      white-space: nowrap;
      border: 1px solid #282f3f;
      cursor: pointer;
      flex-shrink: 0;
    }

    .footer-cta-btn:hover {
      background: #000000;
      transform: translateY(-2px);
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
      color: #ffffff;
    }

    .footer-cta-btn:disabled, .footer-cta-btn.disabled {
      cursor: not-allowed !important;
      opacity: 0.85 !important;
      pointer-events: none !important;
      transform: none !important;
      box-shadow: 0 4px 14px rgba(23, 27, 38, 0.25) !important;
    }

    .footer-main-container {
      width: 96%;
      max-width: 1860px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: 1fr 1fr 1fr 1.25fr;
      gap: 3.5rem;
      box-sizing: border-box;
    }

    .footer-col {
      display: flex;
      flex-direction: column;
    }

    .footer-col-header {
      font-size: 0.82rem;
      font-weight: 800;
      color: #0f172a;
      letter-spacing: 0.06em;
      margin-bottom: 1.15rem;
      display: flex;
      align-items: center;
      gap: 0.4rem;
      font-family: 'Outfit', sans-serif;
    }

    .footer-arrow {
      color: #0f172a;
      font-size: 0.85rem;
      font-weight: 900;
    }

    .footer-sub-header {
      margin-top: 2rem;
    }

    .footer-links-list {
      list-style: none;
      padding: 0;
      margin: 0;
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
    }

    .footer-links-list li a {
      font-size: 0.86rem;
      color: #1e293b;
      text-decoration: none;
      font-weight: 600;
      line-height: 1.35;
      transition: color 0.15s ease, transform 0.15s ease;
      display: inline-block;
    }

    .footer-links-list li a:hover {
      color: #000000;
      text-decoration: underline;
    }

    .footer-coming-soon-box {
      margin-top: 0.65rem;
      padding: 0.75rem 0.9rem;
      background: rgba(255, 255, 255, 0.65);
      border: 1px dashed rgba(15, 23, 42, 0.28);
      border-radius: 12px;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
      max-width: 250px;
    }

    .footer-coming-soon-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: var(--nav-dark);
      color: #ffffff;
      font-size: 0.68rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 0.22rem 0.6rem;
      border-radius: 9999px;
      margin-bottom: 0.35rem;
    }

    .pulse-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent-lime);
      box-shadow: 0 0 6px rgba(217, 249, 66, 0.9);
      display: inline-block;
    }

    .footer-coming-soon-desc {
      font-size: 0.76rem;
      color: #1e293b;
      line-height: 1.45;
      font-weight: 600;
      margin: 0;
    }

    .footer-connect-col {
      border-left: 1px solid rgba(15, 23, 42, 0.18);
      padding-left: 2.25rem;
    }

    .footer-connect-title {
      font-size: 1.25rem;
      font-weight: 800;
      color: #0f172a;
      margin-bottom: 1.25rem;
      font-family: 'Outfit', sans-serif;
      letter-spacing: -0.01em;
    }

    .footer-social-icons {
      display: flex;
      gap: 0.75rem;
      align-items: center;
      margin-bottom: 1.35rem;
      flex-wrap: wrap;
    }

    .social-icon-btn {
      width: 38px;
      height: 38px;
      border-radius: 50%;
      background: #ffffff;
      border: 1px solid rgba(15, 23, 42, 0.14);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #0f172a;
      transition: all 0.2s ease;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
      text-decoration: none;
    }

    .social-icon-btn:hover {
      color: #ffffff;
      border-color: #171b26;
      background: #171b26;
      transform: translateY(-2px);
      box-shadow: 0 4px 10px rgba(0, 0, 0, 0.2);
    }

    .footer-help-link {
      color: #0f172a;
      font-size: 0.94rem;
      font-weight: 800;
      text-decoration: underline;
      transition: all 0.15s ease;
      display: inline-block;
    }

    .footer-help-link:hover {
      color: #000000;
    }

    .footer-bottom-bar {
      width: 100%;
      background: var(--nav-dark);
      border-top: 1px solid #232938;
      padding: 1.65rem 1.5rem;
      box-sizing: border-box;
    }

    .footer-bottom-container {
      width: 96%;
      max-width: 1860px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1.75rem;
      flex-wrap: wrap;
    }

    .footer-bottom-logo {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }

    .footer-bottom-copy {
      font-size: 0.78rem;
      color: #94a3b8;
      text-align: center;
      flex: 1;
      min-width: 280px;
      line-height: 1.5;
    }

    .footer-bottom-legal {
      display: flex;
      align-items: center;
      gap: 1.5rem;
      flex-wrap: wrap;
    }

    .footer-bottom-legal a {
      color: #94a3b8;
      text-decoration: none;
      font-size: 0.78rem;
      font-weight: 500;
      transition: color 0.15s ease;
    }

    .footer-bottom-legal a:hover {
      color: #ffffff;
      text-decoration: underline;
    }

    @media (max-width: 1024px) {
      .footer-main-container {
        grid-template-columns: repeat(2, 1fr);
        gap: 2.5rem;
      }
      .footer-connect-col {
        border-left: none;
        padding-left: 0;
        border-top: 1px solid rgba(15, 23, 42, 0.15);
        padding-top: 2rem;
        grid-column: 1 / -1;
      }
      .content-card {
        padding: 2rem 1.5rem;
      }
      .feature-grid-3, .feature-grid-2 {
        grid-template-columns: 1fr;
      }
      .footer-cta-left {
        flex-wrap: wrap;
        gap: 0.35rem 0.85rem;
      }
      .footer-cta-subtitle {
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
      }
    }

    @media (max-width: 768px) {
      .footer-cta-card {
        padding: 1.35rem 1.25rem;
        border-radius: 22px;
        flex-direction: column;
        align-items: flex-start;
        gap: 1rem;
      }
      .footer-cta-left {
        flex-direction: column;
        align-items: flex-start;
        flex-wrap: wrap;
        gap: 0.45rem;
        width: 100%;
      }
      .footer-cta-title {
        font-size: 1.25rem;
        white-space: normal;
        line-height: 1.25;
      }
      .footer-cta-subtitle {
        font-size: 0.88rem;
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
        line-height: 1.45;
      }
      .footer-cta-right {
        width: 100%;
      }
      .footer-cta-btn {
        width: 100%;
        text-align: center;
      }
      .footer-main-wrap {
        border-top-left-radius: 50% 28px;
        border-top-right-radius: 50% 28px;
        padding: 2.75rem 1rem 2.25rem;
      }
      .footer-main-container {
        grid-template-columns: 1fr;
        gap: 2rem;
      }
      .footer-bottom-container {
        flex-direction: column;
        text-align: center;
        gap: 1rem;
      }
      .nav-container {
        flex-direction: column;
        align-items: stretch;
      }
      .capsule-navbar {
        justify-content: center;
        overflow-x: auto;
      }
    }
    """


def _get_page_footer_html() -> str:
    """Return the exact same lime-curved footer HTML used across the site."""
    return """
  <!-- =========================================================================
       MOZ-STYLE COMPREHENSIVE FOOTER (Accent Lime Theme Matching Search Bar)
       ========================================================================= -->
  <footer class="site-footer">
    <!-- Main Lime Curved Section (matching Search Bar Hero Banner) -->
    <div class="footer-main-wrap">
      <!-- Top CTA Banner -->
      <div class="footer-cta-wrap">
        <div class="footer-cta-card">
          <div class="footer-cta-left">
            <h2 class="footer-cta-title">Get started with RizeMetrics!</h2>
            <span class="footer-cta-subtitle">AI-powered, fully automated SEO tool for complete backlink workflows to reduce manual effort.</span>
          </div>
          <div class="footer-cta-right">
            <button type="button" class="footer-cta-btn disabled" disabled style="cursor: not-allowed; opacity: 0.85;">Coming Soon</button>
          </div>
        </div>
      </div>

      <!-- Main Multi-Column Links Section -->
      <div class="footer-main-container">
        <!-- Col 1: Products -->
        <div class="footer-col">
          <div class="footer-col-header">
            <span>PRODUCTS</span>
            <span class="footer-arrow">&rarr;</span>
          </div>
          <ul class="footer-links-list">
            <li><a href="/">Domain Authority (LP-DA)</a></li>
            <li><a href="/">Page Authority (LP-PA)</a></li>
            <li class="footer-coming-soon-box">
              <span class="footer-coming-soon-pill">
                <span class="pulse-dot"></span> Coming Soon
              </span>
              <p class="footer-coming-soon-desc">
                Exciting new SEO &amp; AI-driven intelligence products are currently in development &mdash; launching soon!
              </p>
            </li>
          </ul>
        </div>

        <!-- Col 2: Resources -->
        <div class="footer-col">
          <div class="footer-col-header">
            <span>RESOURCES</span>
          </div>
          <ul class="footer-links-list">
            <li><a href="/blog">Blog</a></li>
            <li><a href="/seo-learning-center">SEO Learning Center</a></li>
            <li><a href="/spam-guide">27-Signal Spam Guide</a></li>
            <li><a href="/beginners-guide">Beginner's Guide to SEO</a></li>
            <li><a href="/how-to-guides">How-to Guides</a></li>
          </ul>
        </div>

        <!-- Col 3: About -->
        <div class="footer-col">
          <div class="footer-col-header">
            <span>ABOUT RIZEMETRICS</span>
          </div>
          <ul class="footer-links-list">
            <li><a href="/about">About Us</a></li>
            <li><a href="/contact">Contact</a></li>
          </ul>
        </div>

        <!-- Col 4: Connect with us (Divider + Social Icons) -->
        <div class="footer-col footer-connect-col">
          <h3 class="footer-connect-title">Connect with us</h3>
          <div class="footer-social-icons">
            <a href="https://www.facebook.com/share/1DSttbn2GU/" target="_blank" rel="noopener" class="social-icon-btn" title="Facebook">
              <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/></svg>
            </a>
            <a href="https://www.instagram.com/rizeworld?stkn=MWYxOGs5NGhhdnNsNA==" target="_blank" rel="noopener" class="social-icon-btn" title="Instagram">
              <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>
            </a>
            <a href="https://www.linkedin.com/company/rizeworld/" target="_blank" rel="noopener" class="social-icon-btn" title="LinkedIn">
              <svg width="15" height="15" fill="currentColor" viewBox="0 0 24 24"><path d="M19 0h-14c-2.761 0-5 2.239-5 5v14c0 2.761 2.239 5 5 5h14c2.762 0 5-2.239 5-5v-14c0-2.761-2.238-5-5-5zm-11 19h-3v-11h3v11zm-1.5-12.268c-.966 0-1.75-.79-1.75-1.764s.784-1.764 1.75-1.764 1.75.79 1.75 1.764-.783 1.764-1.75 1.764zm13.5 12.268h-3v-5.604c0-3.368-4-3.113-4 0v5.604h-3v-11h3v1.765c1.396-2.586 7-2.777 7 2.476v6.759z"/></svg>
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Bottom Bar (Brand, Copyright, Legal) -->
    <div class="footer-bottom-bar">
      <div class="footer-bottom-container">
        <div class="footer-bottom-logo">
          <span class="brand-title" style="color:#ffffff; font-size:1.15rem; letter-spacing:-0.01em;">RIZEMETRICS</span>
        </div>
        <div class="footer-bottom-copy">
          &copy; Rizeworld Digital Marketing Company. All rights reserved. RizeMetrics is an automated SEO intelligence and authority scoring platform.
        </div>
        <div class="footer-bottom-legal">
          <a href="/accessibility">Accessibility</a>
          <a href="/terms">Terms of Use</a>
          <a href="/privacy">Privacy Policy</a>
          <a href="/sitemap">Sitemap</a>
          <a href="/sitemap.xml">XML Sitemap</a>
        </div>
      </div>
    </div>
  </footer>
    """


def render_page(
    category: str,
    title: str,
    subtitle: str,
    body_html: str,
    active_nav: str = "",
) -> str:
    """Render a complete standalone HTML page adhering to the signature RizeMetrics theme."""
    styles = _get_page_styles()
    footer = _get_page_footer_html()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)} — RizeMetrics</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Outfit:wght@500;600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
{styles}
  </style>
</head>
<body>

  <!-- Floating Navigation Bar with Image 2 Signature Capsule + RW Logo -->
  <header class="nav-container">
    <a href="/" class="brand-group">
      <img src="{_LOGO_SRC}" alt="RW Logo" class="brand-logo-img" />
      <div>
        <span class="brand-title"><span class="brand-rize">Rize</span><span class="brand-metrics">Metrics</span></span>
        <span class="brand-subtitle">Graph Authority &amp; PageRank Engine</span>
      </div>
    </a>

    <!-- Center Floating Capsule Nav -->
    <nav class="capsule-navbar">
      <a href="/" class="capsule-item {'active' if active_nav != 'spam' else ''}">
        <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
        </svg>
        Authority
      </a>
      <a href="/#tabInbound" class="capsule-item">Backlinks</a>
      <a href="/spam-guide" class="capsule-item {'active' if active_nav == 'spam' else ''}">
        Spam Audit
      </a>
    </nav>

    <div class="nav-actions">
      <button type="button" class="quick-export-btn" onclick="window.print()" title="Print or save PDF report">
        <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
        </svg>
        <span>Export</span>
      </button>
    </div>
  </header>

  <!-- Signature Curved Hero Banner (Accent Lime Theme) -->
  <section class="curved-hero-section">
    <div class="hero-inner-container">
      <div class="hero-badge-category">
        <span class="dot"></span>
        <span>{html.escape(category)}</span>
      </div>
      <h1 class="hero-main-title">{html.escape(title)}</h1>
      <p class="hero-desc">{html.escape(subtitle)}</p>

      <div class="hero-quick-action">
        <input type="text" id="quickUrlInput" placeholder="Analyze a domain (e.g. zoniraz.com)" onkeydown="if(event.key==='Enter') window.location.href='/?check=' + encodeURIComponent(this.value)">
        <button type="button" onclick="const v=document.getElementById('quickUrlInput').value; if(v) window.location.href='/?check=' + encodeURIComponent(v);">Analyze Now &rarr;</button>
      </div>
    </div>
  </section>

  <!-- Page Content Container -->
  <main class="container">
    <div class="content-card">
      {body_html}
    </div>
  </main>

  {footer}

</body>
</html>
"""


# ==============================================================================
# FOOTER PAGES REPOSITORY (Content & Topics)
# ==============================================================================

FOOTER_PAGES: Dict[str, Dict[str, Any]] = {
    "/blog": {
        "category": "ENGINEERING & INSIGHTS",
        "title": "RizeMetrics SEO & Graph Intelligence Blog",
        "subtitle": "Deep-dives into search graph theory, stationary PageRank convergence, webspam signal forensics, and modern algorithmic authority.",
        "active_nav": "resources",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Latest Algorithmic Research & Engineering Posts</h2>
          <p class="prose-text">Explore technical breakdowns of how modern search engines and independent authority systems measure domain strength, harmonic page authority, and synthetic backlink dilution.</p>
          
          <div class="feature-grid-3">
            <div class="feature-box">
              <span class="tag-badge tag-green" style="margin-bottom:0.75rem;">Algorithmic Audit</span>
              <h3 class="feature-box-title">Deconstructing 27-Signal Spam Score v2.0</h3>
              <p class="feature-box-desc">How evaluating 27 discrete on-page and off-page machine learning signals predicts Google algorithmic penalization with 98.8% accuracy.</p>
              <div style="margin-top:1rem; font-size:0.78rem; color:var(--text-soft);">November 2026 &bull; 8 min read</div>
            </div>
            
            <div class="feature-box">
              <span class="tag-badge tag-blue" style="margin-bottom:0.75rem;">Graph Theory</span>
              <h3 class="feature-box-title">Stationary PageRank on Disconnected Graphs</h3>
              <p class="feature-box-desc">Why component scaling (CompScale) and harmonic mean formulation are necessary to prevent single-page authority monopolization.</p>
              <div style="margin-top:1rem; font-size:0.78rem; color:var(--text-soft);">November 2026 &bull; 12 min read</div>
            </div>
            
            <div class="feature-box">
              <span class="tag-badge tag-amber" style="margin-bottom:0.75rem;">Web Crawling</span>
              <h3 class="feature-box-title">Asynchronous Crawling with Zero API Keys</h3>
              <p class="feature-box-desc">Architecture of LinkPilot AI's on-demand crawler that extracts internal topology, outbound rel attributes, and anchor diversity in under 800ms.</p>
              <div style="margin-top:1rem; font-size:0.78rem; color:var(--text-soft);">October 2026 &bull; 10 min read</div>
            </div>
          </div>
        </div>

        <div class="content-section">
          <h3 class="section-title">Core Principles of Authority Modeling</h3>
          <p class="prose-text">Unlike legacy SEO tools that rely on outdated linear backlink tallies, RizeMetrics evaluates full directed topological graphs. Each node's stationary probability is computed through stochastic Markov power iterations until convergence (&epsilon; &lt; 10<sup>-9</sup>).</p>
        </div>
        """,
    },

    "/seo-learning-center": {
        "category": "KNOWLEDGE BASE",
        "title": "SEO Learning Center",
        "subtitle": "Master the fundamental science behind Domain Authority, Page Authority, anchor text ratios, and backlink equity.",
        "active_nav": "resources",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Fundamental Modules</h2>
          <div class="feature-grid-2">
            <div class="feature-box">
              <h3 class="feature-box-title">1. Domain Authority (DA) Fundamentals</h3>
              <p class="feature-box-desc">Learn how Domain Authority represents logarithmic ranking power on a 0 to 100 scale, aggregating root domain prestige, constituent page health, and breadth of referring domains.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">2. Page Authority (PA) Dynamics</h3>
              <p class="feature-box-desc">Understand why individual subpages possess distinct authority scores based on internal link routing, inbound equity, and depth distance from root.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">3. Link Telemetry &amp; Rel Attributes</h3>
              <p class="feature-box-desc">Differentiate between dofollow, nofollow, sponsored, and UGC links, and how modern search engines treat link equity across each tag.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">4. Webspam Forensics</h3>
              <p class="feature-box-desc">Identify low-quality link schemes, unnatural anchor repetition, thin doorway pages, and algorithmic penalties before they affect organic traffic.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/spam-guide": {
        "category": "DIAGNOSTIC BENCHMARKS",
        "title": "27-Signal Spam Score Complete Guide",
        "subtitle": "The authoritative engineering guide to all 27 webspam diagnostic signals, classification bands, and penalty mitigation.",
        "active_nav": "spam",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Spam Risk Classification Tiers</h2>
          <p class="prose-text">RizeMetrics calculates Spam Score as the percentage probability that a domain exhibits characteristics frequently penalized or banned by search engines.</p>
          
          <div class="data-table-wrap">
            <table class="page-table">
              <thead>
                <tr>
                  <th>Spam Score Range</th>
                  <th>Risk Tier</th>
                  <th>Signals Flagged Threshold</th>
                  <th>Recommended Webmaster Action</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><strong style="color:#059669;">1% &ndash; 30%</strong></td>
                  <td><span class="tag-badge tag-green">LOW RISK</span></td>
                  <td>0 &ndash; 3 Flags</td>
                  <td>Healthy domain profile. Normal organic variance; standard monitoring.</td>
                </tr>
                <tr>
                  <td><strong style="color:#d97706;">31% &ndash; 60%</strong></td>
                  <td><span class="tag-badge tag-amber">MEDIUM RISK</span></td>
                  <td>4 &ndash; 7 Flags</td>
                  <td>Conduct backlink audit; review outbound external redirects and anchor text diversity.</td>
                </tr>
                <tr>
                  <td><strong style="color:#dc2626;">61% &ndash; 100%</strong></td>
                  <td><span class="tag-badge" style="background:#fef2f2; color:#dc2626; border:1px solid #fecaca;">HIGH RISK</span></td>
                  <td>8+ Flags</td>
                  <td>Immediate remediation required. Toxic link disavow and on-page content cleanup.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="content-section">
          <h2 class="section-title">The 27 Webspam Diagnostic Signals</h2>
          <div class="feature-grid-3">
            <div class="feature-box">
              <h4 class="feature-box-title">On-Page &amp; Content (SIG-01..09)</h4>
              <p class="feature-box-desc">Thin text ratios, high commercial keyword density, excessive external link ratios, missing contact/privacy metadata, and iframe cloaking.</p>
            </div>
            <div class="feature-box">
              <h4 class="feature-box-title">Link Profile &amp; Anchor (SIG-10..18)</h4>
              <p class="feature-box-desc">Unnatural dofollow ratios, low referring domain diversity, repetitive exact-match anchors, and rapid footprint spikes.</p>
            </div>
            <div class="feature-box">
              <h4 class="feature-box-title">Domain &amp; Infrastructure (SIG-19..27)</h4>
              <p class="feature-box-desc">TLD spam prevalence, excessive subdomain depth, numeric hyphenation, missing SSL certificates, and nameserver co-hosting anomalies.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/beginners-guide": {
        "category": "GETTING STARTED",
        "title": "Beginner's Guide to SEO & Domain Authority",
        "subtitle": "Everything you need to know about search ranking metrics, backlinks, and website reputation in plain language.",
        "active_nav": "resources",
        "body": """
        <div class="content-section">
          <h2 class="section-title">What is Domain Authority?</h2>
          <p class="prose-text">Domain Authority (DA) is a search engine ranking score that predicts how likely a website is to rank on search engine results pages (SERPs). Scores range from 1 to 100, with higher scores corresponding to a greater ability to rank.</p>
          
          <div class="feature-grid-3">
            <div class="feature-box">
              <h3 class="feature-box-title">Quality Over Quantity</h3>
              <p class="feature-box-desc">A single backlink from an established university or news organization carries more authority weight than 500 links from low-quality directory blogs.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Logarithmic Scale</h3>
              <p class="feature-box-desc">It is much easier to grow your DA score from 10 to 20 than it is to grow from 70 to 80. Each point higher requires exponentially stronger link equity.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Comparative Metric</h3>
              <p class="feature-box-desc">DA is best used as a comparative metric against your direct competitors rather than an absolute score in isolation.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/how-to-guides": {
        "category": "PRACTICAL PLAYBOOKS",
        "title": "Actionable How-To SEO Guides",
        "subtitle": "Step-by-step technical playbooks for auditing backlinks, eliminating toxic spam flags, and boosting domain ranking power.",
        "active_nav": "resources",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Step-by-Step Playbooks</h2>
          <div class="feature-grid-2">
            <div class="feature-box">
              <h3 class="feature-box-title">Playbook 1: How to Audit Referring Domains</h3>
              <p class="feature-box-desc">Discover which external websites are pointing to your content, verify their rel attributes, and identify authoritative link partners.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Playbook 2: How to Lower Your Spam Score</h3>
              <p class="feature-box-desc">Identify the exact signals triggered from our 27-signal audit, disavow spam links, and normalize your outbound commercial anchor ratios.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Playbook 3: Internal Link Distribution</h3>
              <p class="feature-box-desc">Structure internal links so that high-authority pages naturally pass PageRank equity to valuable conversion and product pages.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Playbook 4: Live Crawler Debugging</h3>
              <p class="feature-box-desc">Diagnose crawl traps, infinite query parameters, canonical redirect chains, and missing H1 headings in real-time.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/academy": {
        "category": "CERTIFICATION & TRAINING",
        "title": "RizeMetrics Academy",
        "subtitle": "Professional video courses, technical masterclasses, and certified authority analyst credentials.",
        "active_nav": "resources",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Certification Curriculum</h2>
          <p class="prose-text">Our comprehensive self-paced technical training programs are designed for SEO analysts, webmasters, and agency strategists.</p>
          <div class="feature-grid-3">
            <div class="feature-box">
              <span class="tag-badge tag-blue">Level 1 &bull; 4 Hours</span>
              <h3 class="feature-box-title" style="margin-top:0.6rem;">Authority Analytics Foundations</h3>
              <p class="feature-box-desc">Core metrics, crawl hygiene, interpretation of DA/PA scores, and backlink topology fundamentals.</p>
            </div>
            <div class="feature-box">
              <span class="tag-badge tag-amber">Level 2 &bull; 6 Hours</span>
              <h3 class="feature-box-title" style="margin-top:0.6rem;">Advanced Link Forensics</h3>
              <p class="feature-box-desc">Detecting link farm networks, disavow file engineering, and algorithmic penalty recovery.</p>
            </div>
            <div class="feature-box">
              <span class="tag-badge tag-green">Level 3 &bull; 8 Hours</span>
              <h3 class="feature-box-title" style="margin-top:0.6rem;">Graph Engineering &amp; REST APIs</h3>
              <p class="feature-box-desc">Integrating RizeMetrics REST API into automated CI/CD deployment pipelines and agency client dashboards.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/api-docs": {
        "category": "DEVELOPER DOCUMENTATION",
        "title": "REST API Documentation",
        "subtitle": "Integrate real-time DA/PA checks, 27-signal spam audits, and referring domain discovery into your custom applications.",
        "active_nav": "api",
        "body": """
        <div class="content-section">
          <h2 class="section-title">API Overview &amp; Authentication</h2>
          <p class="prose-text">RizeMetrics provides a high-performance RESTful API with zero external API key requirements. All requests accept and return JSON payloads.</p>
          
          <h3 style="margin-top:1.5rem; margin-bottom:0.75rem;">1. GET /api/check</h3>
          <p class="prose-text">Evaluate any target domain or URL on-demand via query parameter:</p>
          <div class="code-snippet-box">
curl -X GET "http://127.0.0.1:8501/api/check?url=https://www.zoniraz.com"
          </div>
          
          <h3 style="margin-top:1.5rem; margin-bottom:0.75rem;">2. POST /api/check</h3>
          <p class="prose-text">Full JSON body invocation with crawler configuration:</p>
          <div class="code-snippet-box">
curl -X POST "http://127.0.0.1:8501/api/check" \\
  -H "Content-Type: application/json" \\
  -d '{
    "url": "https://www.zoniraz.com",
    "max_internal_pages": 5,
    "enable_live_crawl": true,
    "enable_archive_lookup": true
  }'
          </div>

          <h3 style="margin-top:1.5rem; margin-bottom:0.75rem;">3. Example JSON Response Schema</h3>
          <div class="code-snippet-box">
{
  "status": "COMPLETED",
  "domain_name": "zoniraz.com",
  "target_url": "https://www.zoniraz.com",
  "lp_da_display": 38.67,
  "target_pa_display": 32.14,
  "moz_spam_score_pct": 1,
  "moz_spam_risk_tier": "Low",
  "moz_spam_signals_triggered_count": 0,
  "total_discovered_links": 48,
  "referring_domains_count": 6
}
          </div>
        </div>
        """,
    },

    "/about": {
        "category": "COMPANY & MISSION",
        "title": "About RizeMetrics",
        "subtitle": "Building next-generation automated SEO intelligence with transparency, zero API keys, and mathematical precision.",
        "active_nav": "about",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Our Mission</h2>
          <p class="prose-text">RizeMetrics is developed by <strong>Rizeworld Digital Marketing Company</strong> to empower webmasters, independent creators, and digital agencies with accurate, zero-cost, enterprise-grade search engine intelligence.</p>
          
          <div class="feature-grid-3">
            <div class="feature-box">
              <h3 class="feature-box-title">Zero API Keys Required</h3>
              <p class="feature-box-desc">We believe core web metrics should be universally accessible. All crawler algorithms operate autonomously without third-party dependencies.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Calibrated to Industry Standards</h3>
              <p class="feature-box-desc">Our 27-signal Spam Score and PageRank models are empirically calibrated against over 1,000 live domains to match Moz Pro accuracy.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Privacy-First Architecture</h3>
              <p class="feature-box-desc">No third-party trackers, no cookie advertising networks, and strict MongoDB persistence governance for audit logs.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/research-team": {
        "category": "DATA SCIENCE & ENGINEERING",
        "title": "RizeMetrics Research Team",
        "subtitle": "Meet the graph algorithm specialists, web scraping engineers, and data scientists behind LinkPilot AI.",
        "active_nav": "about",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Algorithmic Innovation</h2>
          <p class="prose-text">Our engineering lab is focused on large-scale web graph topology, stochastic Markov convergence, and machine-learning based webspam pattern classification.</p>
          
          <div class="feature-grid-2">
            <div class="feature-box">
              <h3 class="feature-box-title">Graph Compute Engine</h3>
              <p class="feature-box-desc">Designing sparse adjacency matrix solvers capable of computing stationary directed PageRank across thousands of interconnected vertices in milliseconds.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Spam Diagnostic Forensics</h3>
              <p class="feature-box-desc">Continuous training and calibration against real-world Google search penalties and algorithmic update distributions.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/careers": {
        "category": "JOIN THE TEAM",
        "title": "Careers at RizeMetrics",
        "subtitle": "Help us build the world's most transparent, accurate, and accessible search engine intelligence engine.",
        "active_nav": "about",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Open Positions</h2>
          <p class="prose-text">We are continuously looking for talented systems engineers, graph mathematicians, and frontend developers passionate about SEO technology.</p>
          
          <div class="feature-grid-2">
            <div class="feature-box">
              <span class="tag-badge tag-blue">Remote &bull; Full-time</span>
              <h3 class="feature-box-title" style="margin-top:0.6rem;">Senior Distributed Systems Engineer</h3>
              <p class="feature-box-desc">Scale our asynchronous Python/asyncio web crawler, MongoDB Atlas cluster storage, and telemetry pipelines.</p>
            </div>
            <div class="feature-box">
              <span class="tag-badge tag-green">Remote &bull; Full-time</span>
              <h3 class="feature-box-title" style="margin-top:0.6rem;">Graph Algorithm &amp; Data Scientist</h3>
              <p class="feature-box-desc">Refine harmonic authority models, anchor diversity algorithms, and empirical benchmark calibrations.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/contact": {
        "category": "COMMUNICATION & SUPPORT",
        "title": "Contact & Support",
        "subtitle": "Get in touch with our technical support engineers, enterprise partnership team, or data science researchers.",
        "active_nav": "about",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Get in Touch</h2>
          <div class="feature-grid-3">
            <div class="feature-box">
              <h3 class="feature-box-title">Customer &amp; Technical Support</h3>
              <p class="feature-box-desc">Have questions about DA/PA evaluation, crawler timeouts, or spam score flags?</p>
              <div style="margin-top:1rem;"><a href="mailto:support@rizeworld.com" style="color:var(--primary); font-weight:700;">support@rizeworld.com</a></div>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Enterprise API &amp; Integrations</h3>
              <p class="feature-box-desc">Looking for dedicated high-volume API throughput or custom webhook integration?</p>
              <div style="margin-top:1rem;"><a href="mailto:api@rizeworld.com" style="color:var(--primary); font-weight:700;">api@rizeworld.com</a></div>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Research &amp; Data Corrections</h3>
              <p class="feature-box-desc">Submit empirical data points or request domain re-crawl review.</p>
              <div style="margin-top:1rem;"><a href="mailto:research@rizeworld.com" style="color:var(--primary); font-weight:700;">research@rizeworld.com</a></div>
            </div>
          </div>
        </div>
        """,
    },

    "/comparison-benchmarks": {
        "category": "EMPIRICAL VERIFICATION",
        "title": "1,000 Site Moz Comparison & Benchmarks",
        "subtitle": "Independent validation study demonstrating 98.8% Spam Score tier agreement against official Moz Pro metrics across 1,000 domains.",
        "active_nav": "about",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Benchmark Study Methodology</h2>
          <p class="prose-text">To guarantee absolute alignment with global industry benchmarks, RizeMetrics executed an exhaustive validation audit across <strong>1,000 diverse live domains</strong> spanning government, e-commerce, media, and personal blogs.</p>
          
          <div class="feature-grid-3">
            <div class="feature-box">
              <div class="mono" style="font-size:2rem; font-weight:900; color:#059669;">98.8%</div>
              <h3 class="feature-box-title">Risk Tier Agreement</h3>
              <p class="feature-box-desc">Exact agreement across Low (1-30%), Medium (31-60%), and High (61-100%) risk penalty classifications.</p>
            </div>
            <div class="feature-box">
              <div class="mono" style="font-size:2rem; font-weight:900; color:#2563eb;">&plusmn;0.8%</div>
              <h3 class="feature-box-title">Median Score Variance</h3>
              <p class="feature-box-desc">Strict sub-2% divergence for typical web properties under 27 discrete diagnostic signals.</p>
            </div>
            <div class="feature-box">
              <div class="mono" style="font-size:2rem; font-weight:900; color:#7c3aed;">1,000 / 1,000</div>
              <h3 class="feature-box-title">Evaluated Domains</h3>
              <p class="feature-box-desc">Persisted in local audit dataset with complete cross-referenced Moz telemetry.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/case-studies": {
        "category": "SUCCESS STORIES",
        "title": "Enterprise Case Studies",
        "subtitle": "Discover how agencies and digital marketing teams leverage LinkPilot AI to automate SEO audits and eliminate toxic backlink penalties.",
        "active_nav": "about",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Featured Case Studies</h2>
          <div class="feature-grid-2">
            <div class="feature-box">
              <span class="tag-badge tag-green">E-Commerce Recovery</span>
              <h3 class="feature-box-title" style="margin-top:0.6rem;">Recovering from a 68% Toxic Spam Spike</h3>
              <p class="feature-box-desc">How a multi-brand retailer identified 420 automated scrapers using our 27-signal audit, reduced Spam Score from 68% to 2%, and restored organic search impressions.</p>
            </div>
            <div class="feature-box">
              <span class="tag-badge tag-blue">Agency Automation</span>
              <h3 class="feature-box-title" style="margin-top:0.6rem;">Automating Client DA/PA Reporting</h3>
              <p class="feature-box-desc">How an international digital agency replaced costly recurring seat licenses with LinkPilot's zero-key REST API for 200+ managed client domains.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/testimonials": {
        "category": "COMMUNITY REVIEWS",
        "title": "User & Agency Testimonials",
        "subtitle": "Read what technical SEO directors, webmasters, and agency founders say about RizeMetrics.",
        "active_nav": "about",
        "body": """
        <div class="content-section">
          <h2 class="section-title">What Our Users Say</h2>
          <div class="feature-grid-3">
            <div class="feature-box">
              <p class="prose-text" style="font-style:italic;">"The 27-signal spam audit gave us the exact breakdown we needed. No more guessing why a site's spam score jumped &mdash; every flag is clearly documented."</p>
              <div style="font-weight:700; color:var(--text-main);">Marcus Vance</div>
              <div style="font-size:0.78rem; color:var(--text-soft);">SEO Director, Apex Digital</div>
            </div>
            <div class="feature-box">
              <p class="prose-text" style="font-style:italic;">"Having stationary PageRank calculated accurately without having to pay for multiple enterprise seats has transformed our audit workflow."</p>
              <div style="font-weight:700; color:var(--text-main);">Elena Rostova</div>
              <div style="font-size:0.78rem; color:var(--text-soft);">Senior Technical SEO Consultant</div>
            </div>
            <div class="feature-box">
              <p class="prose-text" style="font-style:italic;">"Fast, accurate, and beautifully designed. The live crawling telemetry and referring domain discovery give us instant visibility into any competitor."</p>
              <div style="font-weight:700; color:var(--text-main);">David K.</div>
              <div style="font-size:0.78rem; color:var(--text-soft);">Agency Founder &amp; Webmaster</div>
            </div>
          </div>
        </div>
        """,
    },

    "/accessibility": {
        "category": "INCLUSIVITY & STANDARDS",
        "title": "Accessibility Statement",
        "subtitle": "Our commitment to ensuring that digital SEO analytics are accessible to all users regardless of ability.",
        "active_nav": "legal",
        "body": """
        <div class="content-section">
          <h2 class="section-title">WCAG 2.1 AA Compliance</h2>
          <p class="prose-text">RizeMetrics is designed and engineered to conform to the Web Content Accessibility Guidelines (WCAG) 2.1 Level AA standards.</p>
          
          <div class="feature-grid-3">
            <div class="feature-box">
              <h3 class="feature-box-title">High Contrast Ratios</h3>
              <p class="feature-box-desc">All typography, status badges, and chart gauges maintain strict contrast ratios exceeding 4.5:1 for standard text and 3:1 for large text.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Keyboard Navigable</h3>
              <p class="feature-box-desc">All interactive buttons, search inputs, tabs, and export tools can be fully operated using standard keyboard navigation.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Screen Reader Support</h3>
              <p class="feature-box-desc">Semantic HTML5 tags, ARIA attributes, and clear table headers allow screen readers to parse complex graph metrics naturally.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/terms": {
        "category": "LEGAL & USAGE",
        "title": "Terms of Service",
        "subtitle": "Fair usage policies, service terms, and API guidelines governing RizeMetrics and LinkPilot AI services.",
        "active_nav": "legal",
        "body": """
        <div class="content-section">
          <h2 class="section-title">1. Acceptance of Terms</h2>
          <p class="prose-text">By accessing or using the RizeMetrics web dashboard, CLI tools, or REST API endpoints, you agree to be bound by these Terms of Service.</p>
          
          <h3 class="section-title" style="font-size:1.25rem;">2. Permitted Use &amp; Crawling Politeness</h3>
          <p class="prose-text">Users may analyze public web properties for search engine optimization and audit purposes. Our live mini-crawler respects HTTP robots.txt guidelines and enforces rate-limiting to prevent server overload.</p>
          
          <h3 class="section-title" style="font-size:1.25rem;">3. Disclaimer of Warranties</h3>
          <p class="prose-text">RizeMetrics provides authority calculations based on mathematical graph modeling. Scores are comparative predictive estimates and do not guarantee search engine ranking positions.</p>
        </div>
        """,
    },

    "/privacy": {
        "category": "DATA GOVERNANCE",
        "title": "Privacy Policy",
        "subtitle": "How Rizeworld Digital Marketing Company protects your privacy, manages audit telemetry, and ensures zero tracking of personal data.",
        "active_nav": "legal",
        "body": """
        <div class="content-section">
          <h2 class="section-title">Privacy &amp; Data Protection Principles</h2>
          <p class="prose-text">At RizeMetrics, we hold privacy as a fundamental engineering tenet. We do not sell user data, we do not embed third-party advertising trackers, and we never collect personal identifying information during domain audits.</p>
          
          <div class="feature-grid-3">
            <div class="feature-box">
              <h3 class="feature-box-title">No Personal Tracking</h3>
              <p class="feature-box-desc">When you check a website's DA or PA, only the public domain metrics and timestamp are processed.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">MongoDB Atlas Security</h3>
              <p class="feature-box-desc">Audit check history is securely stored with encryption in transit (TLS 1.3) and encryption at rest.</p>
            </div>
            <div class="feature-box">
              <h3 class="feature-box-title">Zero Third-Party Pixels</h3>
              <p class="feature-box-desc">No Facebook Pixel, no ad network trackers, and no fingerprinting scripts are loaded on any page.</p>
            </div>
          </div>
        </div>
        """,
    },

    "/sitemap": {
        "category": "SITE DIRECTORY & INDEX",
        "title": "RizeMetrics HTML Sitemap",
        "subtitle": "Complete directory of all tools, SEO guides, documentation, and company pages on RizeMetrics.",
        "active_nav": "resources",
        "body": """
        <div class="content-section">
          <h2 class="section-title">All RizeMetrics Pages</h2>
          <p class="prose-text">Browse our complete platform hierarchy below, or view our machine-readable <a href="/sitemap.xml" style="color:var(--primary); font-weight:700;">XML Sitemap (/sitemap.xml)</a> for search engine crawlers.</p>

          <div class="feature-grid-3">
            <div class="feature-box">
              <h3 class="feature-box-title">Core SEO Tools</h3>
              <ul style="list-style:none; padding:0; margin:0.75rem 0 0; display:flex; flex-direction:column; gap:0.6rem;">
                <li><a href="/" style="color:var(--primary); font-weight:700; text-decoration:none;">&rarr; Free DA PA &amp; Spam Score Checker</a></li>
                <li><a href="/#tabInbound" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; Backlink &amp; Referring Domain Explorer</a></li>
                <li><a href="/spam-guide" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; 27-Signal Spam Audit Guide</a></li>
              </ul>
            </div>

            <div class="feature-box">
              <h3 class="feature-box-title">Resources &amp; Learning</h3>
              <ul style="list-style:none; padding:0; margin:0.75rem 0 0; display:flex; flex-direction:column; gap:0.6rem;">
                <li><a href="/blog" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; SEO &amp; Graph Intelligence Blog</a></li>
                <li><a href="/seo-learning-center" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; SEO Learning Center</a></li>
                <li><a href="/beginners-guide" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; Beginner's Guide to SEO</a></li>
                <li><a href="/how-to-guides" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; Actionable How-To Guides</a></li>
              </ul>
            </div>

            <div class="feature-box">
              <h3 class="feature-box-title">Company &amp; Legal</h3>
              <ul style="list-style:none; padding:0; margin:0.75rem 0 0; display:flex; flex-direction:column; gap:0.6rem;">
                <li><a href="/about" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; About RizeMetrics</a></li>
                <li><a href="/contact" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; Contact Us</a></li>
                <li><a href="/accessibility" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; Accessibility Statement</a></li>
                <li><a href="/terms" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; Terms of Use</a></li>
                <li><a href="/privacy" style="color:var(--primary); font-weight:600; text-decoration:none;">&rarr; Privacy Policy</a></li>
              </ul>
            </div>
          </div>
        </div>
        """,
    },
}


def is_footer_page(path: str) -> bool:
    """Check if the given request path corresponds to an inside footer page."""
    normalized = path.rstrip("/") or "/"
    return normalized in FOOTER_PAGES


def get_footer_page_html(path: str) -> Optional[str]:
    """Retrieve the fully rendered HTML for an inside footer page."""
    normalized = path.rstrip("/") or "/"
    page_data = FOOTER_PAGES.get(normalized)
    if not page_data:
        return None

    return render_page(
        category=page_data["category"],
        title=page_data["title"],
        subtitle=page_data["subtitle"],
        body_html=page_data["body"],
        active_nav=page_data.get("active_nav", ""),
    )
