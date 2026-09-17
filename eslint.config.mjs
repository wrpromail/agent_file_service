import js from "@eslint/js";
import globals from "globals";

/** @type {import("eslint").Linter.Config[]} */
export default [
  js.configs.recommended,
  {
    files: ["web/shared/js/**/*.js", "web/shared/config.js"],
    ignores: ["web/shared/js/uc-strip.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
      },
    },
    rules: {
      "no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-console": "off",
    },
  },
  {
    files: ["web/shared/js/uc-strip.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        ...globals.browser,
      },
    },
    rules: {
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
  {
    ignores: ["**/node_modules/**", "data/**", "dist/**"],
  },
];
