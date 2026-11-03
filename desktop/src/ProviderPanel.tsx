import { useState } from "react";
import { PROVIDERS, providerRequiresKey, type Provider } from "./wizard";

interface Props {
  /** The provider the running engine is bound to. */
  active: Provider;
  /** Whether that engine was started with a real key. */
  activeKeyed: boolean;
  /** Keys typed so far this session, by provider. Memory only. */
  keys: Partial<Record<Provider, string>>;
  onUse: (provider: Provider, key: string) => void;
  onClose: () => void;
}

/**
 * Change the provider or add its key once the app is running. The engine is
 * bound to one provider when it starts, so "Usar" restarts it; keys already
 * typed stay in memory so going back and forth asks for nothing twice.
 */
export default function ProviderPanel({ active, activeKeyed, keys, onUse, onClose }: Props) {
  const [selected, setSelected] = useState<Provider>(active);
  const [typed, setTyped] = useState("");

  const stored = keys[selected] ?? "";
  const needsKey = providerRequiresKey(selected);
  const unchanged = selected === active && typed.trim() === "" && (activeKeyed || !stored);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!unchanged) onUse(selected, typed.trim() || stored);
  }

  return (
    <form className="card provider-panel" onSubmit={handleSubmit}>
      <h2>Proveedor y clave</h2>
      <label>
        Proveedor
        <select
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value as Provider);
            setTyped("");
          }}
        >
          {PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {p}
              {keys[p] ? " · con clave" : ""}
            </option>
          ))}
        </select>
      </label>
      {needsKey && (
        <label>
          Clave de API
          <input
            type="password"
            autoComplete="off"
            placeholder={
              stored
                ? "Ya hay una clave en esta sesión; escribe otra para reemplazarla"
                : "Solo para esta sesión, nunca se guarda en disco"
            }
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
        </label>
      )}
      <p className="hint">
        Las claves solo viven en memoria mientras la app esté abierta. Cambiar de proveedor
        reinicia el motor.
      </p>
      <div className="actions">
        <button type="submit" className="primary" disabled={unchanged}>
          {needsKey && !typed.trim() && !stored ? "Usar sin clave" : "Usar"}
        </button>
        <button type="button" onClick={onClose}>
          Cerrar
        </button>
      </div>
    </form>
  );
}
