import en from "./en.json";
import es from "./es.json";
import { useUiStore } from "../stores/ui";

const dictionaries = { en, es } as const;
export type TranslationKey = keyof typeof en;

export function useT(): (key: TranslationKey) => string {
  const language = useUiStore((state) => state.language);
  return (key) => dictionaries[language][key];
}
