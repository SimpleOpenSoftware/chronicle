/**
 * Chronicle "Espresso" design-system preset (Tailwind v3).
 *
 * Source of truth: the Chronicle Design System project (claude.ai/design),
 * tokens/*.css. Warm espresso neutrals + a terracotta brand ramp + a
 * forest-green status family. Dark is the default surface.
 *
 * Applied by REMAPPING Tailwind's default color NAMES to the Espresso ramps,
 * so the thousands of existing `bg-gray-800 / text-blue-600 / dark:*` utilities
 * reskin with no per-component edits. Light->dark ordering is preserved, so
 * every hand-paired `x-50 dark:x-900` keeps working.
 *
 *   gray  -> espresso neutrals        blue   -> terracotta (brand accent)
 *   green -> forest (success/verifier) red   -> danger      amber/yellow -> warning
 *   purple-> suggest                  orange -> clay        sky/cyan     -> info-blue
 *   aliases: emerald/teal/lime->forest, zinc/slate/neutral/stone->espresso,
 *            indigo/violet->purple, pink/rose->red
 *
 * IMPORTANT: keep this file identical to the copy in
 * extras/speaker-recognition/webui/chronicle-espresso-preset.js.
 * The two web UIs build in separate Docker contexts, so the preset is
 * duplicated rather than shared via a package.
 */

// warm espresso neutrals (DS gray scale, extended to 950)
const espresso = {
  50: '#f7f3ea', 100: '#f2ece2', 200: '#ddd5c6', 300: '#c9bfae', 400: '#b5a791',
  500: '#6b5f4f', 600: '#42392f', 700: '#2c251d', 800: '#211b15', 900: '#191410', 950: '#120d0a',
}

// brand terracotta (DS terracotta ramp; 50/100/200/800/950 interpolated)
const terracotta = {
  50: '#fbeee7', 100: '#f7dccd', 200: '#f0c3ac', 300: '#ecab93', 400: '#e07856',
  500: '#d2694a', 600: '#c2551f', 700: '#a8471f', 800: '#7c351a', 900: '#3a1f14', 950: '#241009',
}

// forest green — positive / verifier / secondary (part of the palette)
const forest = {
  50: '#eaf3ec', 100: '#d1e7d5', 200: '#a9cfb0', 300: '#8fc79a', 400: '#6f9a5f',
  500: '#4f7d54', 600: '#3f6b47', 700: '#34614a', 800: '#294a39', 900: '#1f3729', 950: '#132218',
}

// danger red (DS red ramp)
const danger = {
  50: '#fbeae7', 100: '#f7d5cf', 200: '#f0b3a8', 300: '#eda093', 400: '#e8735f',
  500: '#dc4a3a', 600: '#c53a2b', 700: '#a32d20', 800: '#7d2419', 900: '#5a1a12', 950: '#360e09',
}

// warning amber (DS amber ramp) — shared by both `amber` and `yellow`
const amber = {
  50: '#fcf4e1', 100: '#f8e7bb', 200: '#f2d488', 300: '#f0c674', 400: '#e6ad3f',
  500: '#d99521', 600: '#b8781a', 700: '#925c15', 800: '#6d4513', 900: '#4a2f0f', 950: '#2b1b08',
}

// warm clay (DS clay/apricot/ochre) — for `orange`
const clay = {
  50: '#fbeee4', 100: '#f6dcc6', 200: '#efc39c', 300: '#e5a86f', 400: '#e59b52',
  500: '#d9822f', 600: '#c26a20', 700: '#9c521c', 800: '#743e18', 900: '#4c2911', 950: '#2c170a',
}

// suggest purple (DS purple)
const suggest = {
  50: '#f4eef8', 100: '#e7d9ef', 200: '#d4bce2', 300: '#c2a0d4', 400: '#a986c4',
  500: '#9169b0', 600: '#775394', 700: '#5f4278', 800: '#48335c', 900: '#312340', 950: '#1d1526',
}

// muted info-blue (DS --info-fg family) — keeps a real "info" hue distinct from the brand
const info = {
  50: '#eef3f7', 100: '#d7e3ec', 200: '#b5cbdd', 300: '#8fb0c9', 400: '#6b93b0',
  500: '#4f7896', 600: '#3f6079', 700: '#344e61', 800: '#2a3d4b', 900: '#1f2c37', 950: '#141d24',
}

/** @type {import('tailwindcss').Config} */
export default {
  theme: {
    extend: {
      colors: {
        // neutrals
        gray: espresso, zinc: espresso, slate: espresso, neutral: espresso, stone: espresso,
        // brand
        blue: terracotta, terracotta,
        // status
        green: forest, emerald: forest, teal: forest, lime: forest, forest,
        red: danger, rose: danger, pink: danger,
        amber, yellow: amber,
        orange: clay, clay,
        purple: suggest, violet: suggest, indigo: suggest,
        sky: info, cyan: info, info,
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'Helvetica', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      borderRadius: {
        sm: '0.25rem', // DS chips (4px)
      },
      boxShadow: {
        // warm-tinted ambient elevation for the espresso surfaces
        sm: '0 1px 2px 0 rgba(20,12,6,.40)',
        DEFAULT: '0 2px 4px -1px rgba(20,12,6,.45)',
        md: '0 6px 14px -4px rgba(20,12,6,.50)',
        lg: '0 12px 28px -6px rgba(20,12,6,.55), 0 4px 10px -4px rgba(20,12,6,.45)',
        xl: '0 14px 36px rgba(20,12,6,.60)',
      },
    },
  },
}
