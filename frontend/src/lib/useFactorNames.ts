import { useEffect, useState } from "react";
import { customers } from "../api";
import { factorNames } from "./factorNames";
import type { FactorNames } from "./factorNames";

let pending: Promise<FactorNames> | null = null;
const empty: FactorNames = new Map();

export function useFactorNames(): FactorNames {
  const [names, setNames] = useState<FactorNames>(empty);
  useEffect(() => {
    let active = true;
    // Share concurrent requests from saved bubbles; later mounts can refresh
    // the current configuration. If unavailable, preserve original identifiers.
    pending ??= Promise.resolve().then(() => customers.factorConfig()).then(factorNames)
      .catch(() => empty).finally(() => { pending = null; });
    void pending.then((value) => { if (active) setNames(value); });
    return () => { active = false; };
  }, []);
  return names;
}
