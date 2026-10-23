import type { EstimateResponse } from "./apiTypes";
import {
  formatBookEstimate,
  formatGlossaryExtractionLine,
  START_TRANSLATION_NOTE,
} from "./wizard";

interface Props {
  estimate: EstimateResponse;
  /** Why the estimate screen is shown again (a preparation that did not
   * finish), or null. */
  preparationMessage: string | null;
  /** Starts the glossary preparation — the first paid step of the book. */
  onStart: () => void;
  /** Goes on to the glossary review once the glossary is prepared. */
  onContinue: () => void;
}

/** The estimate screen: what the whole book costs. The glossary preparation
 * cannot be skipped — a parallel translation freezes the glossary, so a book
 * without one would be translated with none — and starting it is presented
 * as starting the translation. */
export default function EstimateSummary({
  estimate,
  preparationMessage,
  onStart,
  onContinue,
}: Props) {
  return (
    <div>
      <h2>2. Estimación</h2>
      {preparationMessage && <p role="alert">{preparationMessage}</p>}
      <p>{formatBookEstimate(estimate)}</p>
      {!estimate.within_budget && (
        <p role="alert">Esta estimación supera el presupuesto configurado.</p>
      )}
      {estimate.glossary_extraction ? (
        <div>
          <p>{formatGlossaryExtractionLine(estimate.glossary_extraction)}</p>
          <p>{START_TRANSLATION_NOTE}</p>
          <button onClick={onStart}>Comenzar la traducción</button>
        </div>
      ) : (
        <button onClick={onContinue}>Continuar al glosario</button>
      )}
    </div>
  );
}
