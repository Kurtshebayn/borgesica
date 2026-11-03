import type { FormEvent } from "react";
import { PROVIDERS, providerRequiresKey, type Provider } from "./wizard";

interface Props {
  provider: Provider;
  apiKey: string;
  /** True between "Iniciar" and the sidecar reporting ready. */
  booting: boolean;
  error: string | null;
  onProviderChange: (provider: Provider) => void;
  onApiKeyChange: (key: string) => void;
  onStart: () => void;
  /** Enter the library without a key: books can be read, not translated. */
  onStartWithoutKey: () => void;
}

/**
 * The first screen, over the engraving: a ficha where the person picks the
 * provider and types the session-only key. The engine only starts after that,
 * so the same ficha turns into the one real wait in the app once "Iniciar" is
 * pressed.
 */
export default function StartScreen({
  provider,
  apiKey,
  booting,
  error,
  onProviderChange,
  onApiKeyChange,
  onStart,
  onStartWithoutKey,
}: Props) {
  const canStart = !providerRequiresKey(provider) || apiKey.trim() !== "";

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!booting && canStart) onStart();
  }

  return (
    <div className="start">
      <p className="start-kicker">Borgésica / motor</p>
      <h1 className="start-title">Borgésica</h1>
      <p className="start-tagline">
        Un catálogo para los libros que ya leíste en otro idioma.
      </p>
      <form className="start-card" onSubmit={handleSubmit} aria-busy={booting}>
        <div className="start-card-head">
          <span>Ficha</span>
          <span>BAB-····</span>
        </div>
        <label className="start-field">
          <span>Proveedor</span>
          <select
            value={provider}
            disabled={booting}
            onChange={(e) => onProviderChange(e.target.value as Provider)}
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="start-field">
          <span>Clave de API</span>
          <input
            type="password"
            autoComplete="off"
            disabled={booting}
            placeholder={
              provider === "ollama"
                ? "No hace falta para Ollama"
                : "Solo para esta sesión, nunca se guarda"
            }
            value={apiKey}
            onChange={(e) => onApiKeyChange(e.target.value)}
          />
        </label>
        {error && !booting && (
          <p role="alert" className="start-error">
            {error}
          </p>
        )}
        {booting ? (
          <div role="status" aria-live="polite">
            <div className="start-field start-field-live">
              <span>Estado</span>
              <strong>encendiendo el motor</strong>
            </div>
            <div className="start-bar" />
          </div>
        ) : (
          <>
            <button type="submit" className="primary" disabled={!canStart}>
              Iniciar
            </button>
            {providerRequiresKey(provider) && (
              <button type="button" className="link start-skip" onClick={onStartWithoutKey}>
                Entrar sin clave
              </button>
            )}
          </>
        )}
      </form>
    </div>
  );
}
