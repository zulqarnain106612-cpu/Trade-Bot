export default {
  plugins: {
    // Tailwind v4 moved the PostCSS plugin out of the `tailwindcss` package;
    // referencing it directly is what vite build rejects.
    "@tailwindcss/postcss": {},
    autoprefixer: {},
  },
};