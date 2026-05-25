/**
 * Minimal design tokens. Intentionally small — enough to keep screens
 * consistent and to serve as the base for the future ledger / settlement
 * front-ends, without pulling in a UI framework.
 */
export const theme = {
  colors: {
    background: '#0f1419',
    surface: '#1b2530',
    border: '#2c3a47',
    text: '#e8eef2',
    textMuted: '#8a9bab',
    primary: '#3d8bfd',
    primaryText: '#ffffff',
    danger: '#ff6b6b',
    success: '#4cd07d',
    inputBackground: '#11181f',
  },
  spacing: (n: number) => n * 8,
  radius: 10,
  fontSize: {
    sm: 13,
    md: 16,
    lg: 20,
    xl: 26,
  },
} as const;

export type Theme = typeof theme;
