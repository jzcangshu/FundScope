import { theme as antdTheme, type ThemeConfig } from "antd";

/**
 * Design tokens — the single source of truth for colour, type, spacing,
 * radius, shadow and motion. `styles/globals.css` mirrors the colour tokens as
 * CSS custom properties (one block per theme) and `buildAntdTheme()` derives
 * the Ant Design ConfigProvider theme from the same values, so the two never
 * drift apart.
 *
 * Visual language ("墨 + 朱砂"):
 *  - Neutrals are warm off-whites and inks. The page is a desk, each route is
 *    one sheet of paper (`.fd-page`); sections are separated by hairlines, not
 *    nested cards.
 *  - `primary` is ink: primary buttons, selected states, chart lines.
 *  - `accent` is vermilion (朱砂): the brand seal, alerts and gains. It is the
 *    only saturated colour and is used sparingly.
 *  - `up` is red and `down` is green (红涨绿跌).
 *  - Light is the primary theme; dark is a restrained night variant.
 */
export type ThemeMode = "dark" | "light";

export interface Palette {
  page: string;
  panel: string;
  panelSoft: string;
  panelRaised: string;
  sidebar: string;
  sidebarText: string;
  sidebarTextStrong: string;
  sidebarBorder: string;
  text: string;
  textStrong: string;
  muted: string;
  mutedStrong: string;
  border: string;
  borderStrong: string;
  primary: string;
  primaryHover: string;
  primaryContrast: string;
  primarySoft: string;
  accent: string;
  accentHover: string;
  accentSoft: string;
  accentText: string;
  up: string;
  upSoft: string;
  down: string;
  downSoft: string;
  success: string;
  successSoft: string;
  warning: string;
  warningSoft: string;
  danger: string;
  dangerSoft: string;
  info: string;
  infoSoft: string;
  chart: string;
  chartSecondary: string;
  focus: string;
  shadowSheet: string;
  shadowPopover: string;
  shadowDialog: string;
  scrim: string;
}

export const lightPalette: Palette = {
  page: "#eeece7",
  panel: "#ffffff",
  panelSoft: "#f7f6f2",
  panelRaised: "#ffffff",
  sidebar: "transparent",
  sidebarText: "#5d5a54",
  sidebarTextStrong: "#1c1b19",
  sidebarBorder: "rgba(28, 27, 25, 0.08)",
  text: "#2b2a27",
  textStrong: "#1c1b19",
  muted: "#75726b",
  mutedStrong: "#4f4c46",
  border: "#e9e7e1",
  borderStrong: "#d4d1c9",
  primary: "#1c1b19",
  primaryHover: "#37352f",
  primaryContrast: "#ffffff",
  primarySoft: "rgba(28, 27, 25, 0.06)",
  accent: "#c8412a",
  accentHover: "#ad3520",
  accentSoft: "rgba(200, 65, 42, 0.1)",
  accentText: "#b83a24",
  up: "#c8412a",
  upSoft: "rgba(200, 65, 42, 0.1)",
  down: "#2c7a58",
  downSoft: "rgba(44, 122, 88, 0.1)",
  success: "#2c7a58",
  successSoft: "rgba(44, 122, 88, 0.1)",
  warning: "#a86d13",
  warningSoft: "rgba(168, 109, 19, 0.12)",
  danger: "#c8412a",
  dangerSoft: "rgba(200, 65, 42, 0.1)",
  info: "#4b6a89",
  infoSoft: "rgba(75, 106, 137, 0.1)",
  chart: "#1c1b19",
  chartSecondary: "#c8412a",
  focus: "rgba(200, 65, 42, 0.35)",
  shadowSheet: "0 1px 2px rgba(28, 27, 25, 0.05), 0 12px 32px -20px rgba(28, 27, 25, 0.16), 0 40px 96px -40px rgba(28, 27, 25, 0.34)",
  shadowPopover: "0 8px 28px -8px rgba(28, 27, 25, 0.18), 0 1px 2px rgba(28, 27, 25, 0.06)",
  shadowDialog: "0 32px 80px -24px rgba(28, 27, 25, 0.35)",
  scrim: "rgba(28, 27, 25, 0.4)",
};

export const darkPalette: Palette = {
  page: "#131311",
  panel: "#1b1b19",
  panelSoft: "#22221f",
  panelRaised: "#282825",
  sidebar: "transparent",
  sidebarText: "#a19d95",
  sidebarTextStrong: "#f2f0eb",
  sidebarBorder: "#2a2a27",
  text: "#e3e0d9",
  textStrong: "#f5f3ee",
  muted: "#948f86",
  mutedStrong: "#bbb6ad",
  border: "#2a2a27",
  borderStrong: "#3c3b37",
  primary: "#eeece6",
  primaryHover: "#ffffff",
  primaryContrast: "#1c1b19",
  primarySoft: "rgba(238, 236, 230, 0.08)",
  accent: "#e05a43",
  accentHover: "#ec6f5a",
  accentSoft: "rgba(224, 90, 67, 0.16)",
  accentText: "#ea6d58",
  up: "#e05a43",
  upSoft: "rgba(224, 90, 67, 0.16)",
  down: "#5fb08a",
  downSoft: "rgba(95, 176, 138, 0.16)",
  success: "#5fb08a",
  successSoft: "rgba(95, 176, 138, 0.16)",
  warning: "#d9a54a",
  warningSoft: "rgba(217, 165, 74, 0.16)",
  danger: "#e05a43",
  dangerSoft: "rgba(224, 90, 67, 0.16)",
  info: "#8ea6c2",
  infoSoft: "rgba(142, 166, 194, 0.16)",
  chart: "#e3e0d9",
  chartSecondary: "#e05a43",
  focus: "rgba(224, 90, 67, 0.45)",
  shadowSheet: "0 1px 2px rgba(0, 0, 0, 0.3)",
  shadowPopover: "0 12px 32px rgba(0, 0, 0, 0.45)",
  shadowDialog: "0 24px 64px rgba(0, 0, 0, 0.55)",
  scrim: "rgba(0, 0, 0, 0.6)",
};

export const palettes: Record<ThemeMode, Palette> = { dark: darkPalette, light: lightPalette };

/** Type, spacing, radius and motion scales shared by both themes. */
export const scale = {
  /** Latin/figures render in Inter (self-hosted); CJK falls through to the system sans. */
  fontFamily:
    '"Inter Variable", Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif',
  fontFamilyMono: '"JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace',
  fontSize: 16,
  radiusControl: 6,
  radiusCard: 10,
  controlHeight: 38,
  motionFast: "120ms",
  motionBase: "200ms",
  ease: "cubic-bezier(0.2, 0.8, 0.2, 1)",
} as const;

export function buildAntdTheme(mode: ThemeMode): ThemeConfig {
  const p = palettes[mode];
  const dark = mode === "dark";
  const fill = (alpha: number) => (dark ? `rgba(255, 255, 255, ${alpha})` : `rgba(28, 27, 25, ${alpha})`);
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: p.primary,
      colorPrimaryHover: p.primaryHover,
      colorPrimaryActive: p.primaryHover,
      colorInfo: p.info,
      colorSuccess: p.success,
      colorWarning: p.warning,
      colorError: p.danger,
      colorLink: p.textStrong,
      colorLinkHover: p.accentText,
      colorLinkActive: p.accentText,
      colorTextBase: p.text,
      colorText: p.text,
      colorTextHeading: p.textStrong,
      colorTextSecondary: p.mutedStrong,
      colorTextTertiary: p.muted,
      colorTextQuaternary: p.muted,
      colorTextPlaceholder: p.muted,
      colorTextDisabled: dark ? "rgba(227, 224, 217, 0.38)" : "rgba(43, 42, 39, 0.38)",
      colorBgBase: p.page,
      colorBgLayout: p.page,
      colorBgContainer: p.panel,
      colorBgElevated: p.panelRaised,
      colorBgSpotlight: p.textStrong,
      colorBgMask: p.scrim,
      colorFill: fill(dark ? 0.1 : 0.07),
      colorFillSecondary: fill(dark ? 0.07 : 0.05),
      colorFillTertiary: fill(dark ? 0.045 : 0.035),
      colorFillQuaternary: fill(dark ? 0.03 : 0.02),
      colorFillAlter: p.panelSoft,
      colorBorder: p.borderStrong,
      colorBorderSecondary: p.border,
      colorSplit: p.border,
      borderRadius: scale.radiusControl,
      borderRadiusLG: scale.radiusCard,
      borderRadiusSM: 4,
      fontFamily: scale.fontFamily,
      fontFamilyCode: scale.fontFamilyMono,
      fontSize: scale.fontSize,
      fontSizeHeading1: 28,
      fontSizeHeading2: 20,
      fontSizeHeading3: 16,
      lineHeight: 1.55,
      controlHeight: scale.controlHeight,
      controlOutline: p.focus,
      controlOutlineWidth: 2,
      boxShadow: p.shadowPopover,
      boxShadowSecondary: p.shadowPopover,
      motionDurationFast: scale.motionFast,
      motionDurationMid: scale.motionBase,
      motionDurationSlow: "260ms",
      wireframe: false,
    },
    components: {
      Layout: { bodyBg: p.page, headerBg: p.panel, siderBg: p.sidebar },
      Card: {
        paddingLG: 20,
        headerHeight: 48,
        headerFontSize: 15,
        colorBorderSecondary: p.border,
        colorBgContainer: p.panel,
        boxShadowTertiary: "none",
      },
      Table: {
        headerBg: "transparent",
        headerColor: p.muted,
        headerSplitColor: "transparent",
        rowHoverBg: p.panelSoft,
        borderColor: p.border,
        cellPaddingInline: 12,
        cellPaddingBlock: 12,
        cellPaddingBlockSM: 12,
        cellPaddingInlineSM: 12,
        cellFontSizeSM: 15,
        headerBorderRadius: 0,
        footerBg: "transparent",
      },
      Button: {
        primaryColor: p.primaryContrast,
        primaryShadow: "none",
        defaultShadow: "none",
        dangerShadow: "none",
        defaultBg: p.panel,
        defaultColor: p.text,
        defaultBorderColor: p.borderStrong,
        defaultHoverBg: p.panelSoft,
        defaultHoverColor: p.textStrong,
        defaultHoverBorderColor: p.textStrong,
        defaultActiveBg: p.panelSoft,
        textHoverBg: fill(dark ? 0.07 : 0.05),
        fontWeight: 500,
        contentFontSizeSM: 14,
      },
      Tag: {
        defaultBg: p.panelSoft,
        defaultColor: p.mutedStrong,
        borderRadiusSM: 4,
      },
      Modal: {
        contentBg: p.panel,
        headerBg: p.panel,
        titleFontSize: 18,
        boxShadow: p.shadowDialog,
      },
      Drawer: { colorBgElevated: p.panel },
      Input: {
        colorBgContainer: p.panel,
        activeBorderColor: p.textStrong,
        hoverBorderColor: p.borderStrong,
        activeShadow: `0 0 0 3px ${p.primarySoft}`,
      },
      InputNumber: {
        colorBgContainer: p.panel,
        activeBorderColor: p.textStrong,
        hoverBorderColor: p.borderStrong,
        activeShadow: `0 0 0 3px ${p.primarySoft}`,
      },
      Select: {
        colorBgContainer: p.panel,
        optionSelectedBg: p.primarySoft,
        optionActiveBg: fill(dark ? 0.07 : 0.045),
        activeBorderColor: p.textStrong,
        hoverBorderColor: p.borderStrong,
      },
      DatePicker: {
        colorBgContainer: p.panel,
        activeBorderColor: p.textStrong,
        hoverBorderColor: p.borderStrong,
        cellActiveWithRangeBg: p.primarySoft,
      },
      Tabs: {
        itemColor: p.muted,
        itemSelectedColor: p.textStrong,
        itemHoverColor: p.textStrong,
        inkBarColor: p.textStrong,
        titleFontSize: 14,
      },
      Segmented: {
        itemColor: p.muted,
        itemHoverColor: p.textStrong,
        itemSelectedColor: p.textStrong,
        itemSelectedBg: p.panel,
        trackBg: p.panelSoft,
        trackPadding: 3,
      },
      Descriptions: {
        labelBg: "transparent",
        titleColor: p.textStrong,
        colorTextSecondary: p.muted,
      },
      Pagination: {
        itemBg: "transparent",
        itemActiveBg: p.primarySoft,
        colorPrimary: p.textStrong,
        colorPrimaryHover: p.textStrong,
      },
      Alert: {
        colorInfoBg: p.infoSoft,
        colorInfoBorder: "transparent",
        colorSuccessBg: p.successSoft,
        colorSuccessBorder: "transparent",
        colorWarningBg: p.warningSoft,
        colorWarningBorder: "transparent",
        colorErrorBg: p.dangerSoft,
        colorErrorBorder: "transparent",
        defaultPadding: "10px 14px",
        withDescriptionPadding: "14px 16px",
      },
      Progress: { defaultColor: p.primary, remainingColor: p.panelSoft },
      Statistic: { titleFontSize: 13, contentFontSize: 26 },
      Collapse: { headerBg: p.panelSoft, contentBg: p.panel },
      Empty: { colorTextDescription: p.muted },
      Tooltip: { colorBgSpotlight: p.textStrong, colorTextLightSolid: p.page },
      Dropdown: { paddingBlock: 6 },
      Avatar: { colorTextPlaceholder: p.primarySoft },
      Skeleton: {
        gradientFromColor: fill(dark ? 0.05 : 0.05),
        gradientToColor: fill(dark ? 0.11 : 0.1),
      },
    },
  };
}
