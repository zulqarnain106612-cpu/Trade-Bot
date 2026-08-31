// Tailwind v4: the PostCSS plugin lives in its own package, and vendor
// prefixing is built in, so autoprefixer is no longer listed.
export default {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
