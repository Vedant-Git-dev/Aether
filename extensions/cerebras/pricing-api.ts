import {
  normalizeModelPricingCatalog,
  normalizeOpenRouterModelPricing,
} from "aether/plugin-sdk/model-catalog-pricing";
import { asOptionalRecord } from "aether/plugin-sdk/string-coerce-runtime";

export function parseCerebrasPricingCatalog(payload: unknown) {
  return normalizeModelPricingCatalog(
    asOptionalRecord(payload)?.data,
    normalizeOpenRouterModelPricing,
  );
}
