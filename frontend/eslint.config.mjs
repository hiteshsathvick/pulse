// eslint-config-next 16 ships native flat configs; the old
// FlatCompat({...}).extends("next/core-web-vitals", "next/typescript")
// shim (for eslintrc-style shareable configs) throws
// "Converting circular structure to JSON" under this ESLint/plugin-react
// combination -- a pre-existing bug in the Phase 0 scaffold, not
// something Phase 14 introduced (reproduced against the untouched
// scaffold before writing this fix).
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const eslintConfig = [...nextCoreWebVitals, ...nextTypescript];

export default eslintConfig;
