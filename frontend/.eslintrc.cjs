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
    "standard",
    "prettier"
  ],
  plugins: ["react", "react-hooks"],
  rules: {
    "react/react-in-jsx-scope": "off",
    "react/prop-types": "off"
  }
}
