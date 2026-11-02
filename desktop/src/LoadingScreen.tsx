/**
 * Shown between "Iniciar" and the sidecar reporting ready. The engine only
 * starts after the person supplies a provider and key, so this is the one
 * real wait in the app. It is a ficha being filled in, over the engraving.
 */
export default function LoadingScreen() {
  return (
    <div className="loading" role="status" aria-live="polite">
      <p className="loading-kicker">Borgésica / motor</p>
      <h1 className="loading-title">Borgésica</h1>
      <p className="loading-tagline">
        Un catálogo para los libros que ya leíste en otro idioma.
      </p>
      <div className="loading-card" aria-hidden="true">
        <div className="loading-card-head">
          <span>Ficha</span>
          <span>BAB-····</span>
        </div>
        <div className="loading-field">
          <span>Título</span>
        </div>
        <div className="loading-field">
          <span>Fragmentos</span>
        </div>
        <div className="loading-field loading-field-live">
          <span>Estado</span>
          <strong>encendiendo el motor</strong>
        </div>
        <div className="loading-bar" />
      </div>
    </div>
  );
}
