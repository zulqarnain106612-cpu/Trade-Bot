module.exports = {
  root: true,
  env: {
    browser: true,
    es2024: true,
    node: true
  },
  parserOptions: {
    ecmaVersion: 2024,
    sourceType: "module",
    ecmaFeatures: {
      jsx: true
    }
  },
  settings: {
    react: {
      version: "detect"
    }
  },
  extends: [
    "eslint:recommended",
    "plugin:react/recommended",
    "plugin:react-hooks/recommended",
    // eslint-config-standard was dropped: it pins peer eslint ^8.0.1 and has no
    // eslint 10 release, so `npm ci` could not resolve the tree at all. Its
    // rules were almost entirely stylistic and are already covered by prettier.
    "prettier"
  ],
  plugins: ["react", "react-hooks"],
  rules: {
    "react/react-in-jsx-scope": "off",
    "react/prop-types": "off"
  }
}
