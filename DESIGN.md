---
version: alpha
name: FTSeek-US Dashboard
description: Dark trading dashboard — GitHub Primer-inspired palette, data-dense, monospace numerals. All tokens below are extracted from the live codebase (dashboard.css / battle_plan.css / base.html) so the spec matches what is actually rendered.
colors:
  primary: "#58A6FF"
  primary-muted: "#60A5FA"
  secondary: "#8B949E"
  success: "#4ADE80"
  success-strong: "#22C55E"
  danger: "#F87171"
  danger-strong: "#F85149"
  danger-bright: "#EF4444"
  warning: "#FBBF24"
  warning-strong: "#D29922"
  warning-bright: "#FACC15"
  info: "#A78BFA"
  cyan: "#2DD4BF"
  bg-deep: "#0F1117"
  bg-surface: "#161B22"
  bg-elevated: "#1C2129"
  bg-hover: "#0D1117"
  border: "#30363D"
  border-subtle: "#21262D"
  text-primary: "#E1E4E8"
  text-secondary: "#8B949E"
  text-dim: "#7D8590"
  text-muted: "#6E7681"
  text-bright: "#F0F6FC"
  white: "#FFFFFF"
  black: "#000000"
  danger-bg: "#3A1A1A"
  danger-bg-soft: "#2A0A0A"
  success-bg-soft: "#0A2A1A"
  warning-bg-soft: "#2A1F0A"
typography:
  body:
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
    fontSize: 14px
    lineHeight: 1.5
  mono:
    fontFamily: '"IBM Plex Mono", monospace'
    fontSize: 12px
    lineHeight: 1.4
  numeric:
    fontFamily: '"IBM Plex Mono", monospace'
    fontSize: 13px
  table-header:
    fontFamily: '"IBM Plex Mono", monospace'
    fontSize: 9px
    fontWeight: 600
    letterSpacing: 0.1em
rounded:
  sm: 4px
  md: 8px
  lg: 10px
  xl: 12px
  pill: 20px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
components:
  pill:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.pill}"
    padding: 5px 12px
  pill-danger:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.danger}"
    rounded: "{rounded.pill}"
    padding: 5px 12px
  pill-warning:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.warning}"
    rounded: "{rounded.pill}"
    padding: 5px 12px
  pill-success:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.success}"
    rounded: "{rounded.pill}"
    padding: 5px 12px
  card:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.lg}"
  card-hover:
    backgroundColor: "{colors.bg-hover}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.lg}"
  card-elevated:
    backgroundColor: "{colors.bg-elevated}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.lg}"
  page-background:
    backgroundColor: "{colors.bg-deep}"
  status-dot-ok:
    backgroundColor: "{colors.success}"
  status-dot-warn:
    backgroundColor: "{colors.warning}"
  status-dot-danger:
    backgroundColor: "{colors.danger}"
  table-header:
    textColor: "{colors.text-muted}"
    typography: "{typography.table-header}"
  text-secondary-label:
    textColor: "{colors.text-secondary}"
  text-dim-label:
    textColor: "{colors.text-dim}"
  text-bright-label:
    textColor: "{colors.text-bright}"
  text-white-label:
    textColor: "{colors.white}"
  text-black-label:
    textColor: "{colors.black}"
  badge-info:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.info}"
    rounded: "{rounded.sm}"
    padding: 1px 6px
  badge-cyan:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.cyan}"
    rounded: "{rounded.sm}"
    padding: 1px 6px
  tag-danger-bg:
    backgroundColor: "{colors.danger-bg}"
    textColor: "{colors.danger}"
    rounded: "{rounded.sm}"
  tag-danger-soft:
    backgroundColor: "{colors.danger-bg-soft}"
    textColor: "{colors.danger}"
    rounded: "{rounded.sm}"
  tag-success-soft:
    backgroundColor: "{colors.success-bg-soft}"
    textColor: "{colors.success}"
    rounded: "{rounded.sm}"
  tag-warning-soft:
    backgroundColor: "{colors.warning-bg-soft}"
    textColor: "{colors.warning}"
    rounded: "{rounded.sm}"
  text-success-strong:
    textColor: "{colors.success-strong}"
  text-danger-strong:
    textColor: "{colors.danger-strong}"
  text-warning-strong:
    textColor: "{colors.warning-strong}"
  text-primary-muted:
    textColor: "{colors.primary-muted}"
  text-danger-bright:
    textColor: "{colors.danger-bright}"
  text-warning-bright:
    textColor: "{colors.warning-bright}"
---

## Overview

FTSeek-US is a real-money US equity trading dashboard. The design is a dark,
data-dense GitHub Primer-inspired interface: deep navy surfaces, blue primary
action color, green/red/yellow semantic status colors, and monospace numerals
for all financial figures. The primary design goal is **information density
without confusion** — a trader must be able to scan equity, positions, PnL,
and risk state in under 3 seconds.

## Colors

- **Primary (#58A6FF):** The single action color — links, active nav, focus
  states, buy-adjacent accents. Never use for loss states.
- **Success (#4ADE80) / Danger (#F87171) / Warning (#FBBF24):** The three
  semantic states. Green = profit/positive, Red = loss/negative, Yellow =
  warning/attention. These three are the only colors that should carry
  financial meaning.
- **Info (#A78BFA) / Cyan (#2DD4BF):** Secondary annotations (debate tags,
  experimental mechanisms). Neutral, carry no financial meaning.
- **Backgrounds:** `bg-deep (#0F1117)` page base, `bg-surface (#161B22)`
  cards, `bg-elevated (#1C2129)` modals/hover. Borders `border (#30363D)`
  strong and `border-subtle (#21262D)` subtle — used as card borders and
  dividers (referenced in Layout & Elevation sections).
- **Text:** `text-primary (#E1E4E8)` body, `text-secondary (#8B949E)` labels,
  `text-dim (#7D8590)` muted. `#7D8590` is dim and should only be used for
  non-essential metadata.

## Typography

- **Body:** system-ui stack (`-apple-system, Segoe UI, Roboto`) — fast, native,
  no webfont cost on the critical dashboard page.
- **Mono (`IBM Plex Mono`):** all numeric values, table headers, and
  technical data. `font-variant-numeric: tabular-nums` on all financial cells
  so digits align vertically (already applied globally in dashboard.css).
- **Display:** system-ui bold for card titles and section headers.

## Layout & Spacing

- 4px base grid. Cards use `12-16px` padding, `8px` gap between cards.
- Navigation adapts at 3 breakpoints: ≥1024 left sidebar, 768-1023 top tabs,
  <768 bottom tab bar (trade centered + raised).

## Elevation & Depth

- Cards are flat (border `#30363D`, no shadow) — depth comes from background
  layering, not shadows.
- Hover states: `bg-hover (#0D1117)` — darker than surface, not lighter.

## Shapes

- `4px` small controls, `8px` cards, `10-12px` large panels, `20px` pills.
- Status dots are `50%` circles.

## Components

- **Status strip:** row of pills (account PnL, positions, equity, VIX, SPY,
  margin, kill switch). Core pill = larger, secondary pills = small/dimmed.
- **Card:** `bg-surface` + `border-subtle`, `12px` padding, `10px` radius.
- **Tables:** mono 9px uppercase headers with `0.1em` letter-spacing, 12px
  body, zebra-free (borders only), right-aligned numerals.
- **Concentration bars:** green/yellow/red fill on `border-subtle` track.
- **Heatmap:** green→red cells with symbol + PnL, click to drill down.

## Do's and Don'ts

- **Do** use `#8B949E` for secondary labels and `#7D8590` for tertiary.
- **Don't** use dim text for anything a trader must act on (stop-loss alerts,
  kill switch state) — those must be `#E1E4E8` or a semantic color.
- **Do** keep green/red strictly financial — never use green for "system ok"
  when it could be read as profit. (Current codebase uses green for system
  OK dots — acceptable but should be clearly scoped to status pills.)
- **Don't** introduce new hex colors outside this token file. If a new color
  is needed, add it here first, then reference it.
- **Do** use tabular-nums for all numeric columns.
- **Don't** stack more than 2 text-contrast levels inside a card (primary +
  secondary; dim only for timestamps/ids).