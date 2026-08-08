// 🔑 A SHORT RULE SET, CHOSEN FOR WHAT `tsc` CANNOT SEE.
//
// `npm run build` already runs `tsc --noEmit`, so type errors are caught. Adding a linter
// only earns its place by finding a different class of defect, and for a React codebase
// that class is almost entirely one thing: **hook dependency arrays**. A stale dependency
// produces a component that reads yesterday's state, renders perfectly, and is very hard to
// reason about from the symptom. No type checker catches it.
//
// Everything stylistic is left out on purpose. A linter that reports two hundred quote and
// semicolon findings teaches everyone to run it with --fix and stop reading the output,
// which costs more than it buys.
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default tseslint.config(
  {
    // `.venv` holds vendored JavaScript inside Python packages, which is not this
    // project's code and produced 12 of the first run's 16 findings.
    ignores: [
      "dist/**",
      "node_modules/**",
      "public/**",
      ".venv/**",
      ".build/**",
      "**/*.config.js",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,

      // An unused variable is usually a leftover from an edit that did not finish. The
      // underscore escape is there for deliberate placeholders, which do occur when
      // destructuring an array and wanting only its tail.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],

      // ⚠️ `any` IS A WARNING, NOT AN ERROR, and that is a considered position rather than
      // laziness. Map and GeoJSON libraries hand back genuinely loosely-typed structures,
      // and forcing a cast at every boundary produces types that assert more than anyone
      // has checked. A warning keeps them visible without making the build fail on them.
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  {
    // Playwright specs run in Node and legitimately use its globals.
    files: ["tests/**/*.ts", "scripts/**/*.mjs"],
    languageOptions: {
      globals: { process: "readonly", console: "readonly", __dirname: "readonly" },
    },
    rules: { "@typescript-eslint/no-explicit-any": "off" },
  },
);
