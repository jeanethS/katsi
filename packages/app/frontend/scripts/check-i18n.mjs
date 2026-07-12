import en from "../src/i18n/en.json" with { type: "json" };
import es from "../src/i18n/es.json" with { type: "json" };

const enKeys = Object.keys(en).sort();
const esKeys = Object.keys(es).sort();
if (JSON.stringify(enKeys) !== JSON.stringify(esKeys)) {
  throw new Error("English and Spanish translation keys differ.");
}
