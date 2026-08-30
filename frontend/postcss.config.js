// Tailwind v3: the PostCSS plugin ships inside the `tailwindcss` package.
// (Do not switch to "@tailwindcss/postcss" without also moving src/index.css
// off the v3 `@tailwind base/components/utilities` directives.)
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
