/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        claude: {
          bg: "#08070a",
          void: "#000000",
          surface: "#141215",
          surface2: "#1c1a1e",
          surface3: "#26232a",
          border: "#2e2b31",
          borderLight: "#3a363e",
          text: "#f2efe9",
          muted: "#a8a29a",
          faint: "#726e69",
          orange: "#da7756",
          orangeDark: "#c25f3d",
          orangeLight: "#e8a084",
          cream: "#faf9f5",
        },
      },
      boxShadow: {
        claude: "0 0 0 1px rgba(218,119,86,0.15), 0 8px 24px -8px rgba(0,0,0,0.6)",
        glow: "0 0 20px -2px rgba(218,119,86,0.35)",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};
