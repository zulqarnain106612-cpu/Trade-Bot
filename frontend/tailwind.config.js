/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        claude: {
          bg: "#050507",
          void: "#000000",
          surface: "#0c0b0f",
          surface2: "#131216",
          surface3: "#1a181e",
          border: "#262329",
          borderLight: "#332f38",
          text: "#e8e5df",
          muted: "#9a958e",
          faint: "#5e5a55",
          orange: "#da7756",
          orangeDark: "#c25f3d",
          orangeLight: "#e8a084",
          cream: "#faf9f5",
          cyan: "#00d4ff",
          cyanDark: "#00a8cc",
          blue: "#007aff",
          blueDark: "#0055b3",
          silver: "#c0c0c0",
          purple: "#a855f7",
        },
      },
      boxShadow: {
        claude: "0 0 0 1px rgba(0,212,255,0.08), 0 8px 24px -8px rgba(0,0,0,0.6)",
        glow: "0 0 20px -2px rgba(0,212,255,0.25)",
        "glow-orange": "0 0 20px -2px rgba(218,119,86,0.25)",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};
