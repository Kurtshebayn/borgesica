import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import EstimateSummary from "./EstimateSummary";
import type { EstimateResponse, GlossaryExtractionEstimate } from "./apiTypes";

const extraction: GlossaryExtractionEstimate = {
  batches: 57,
  input_tokens: 1000,
  output_tokens: 100,
  output_tokens_high: 200,
  usd: 0.12,
  usd_high: 0.57,
};

function estimate(overrides: Partial<EstimateResponse> = {}): EstimateResponse {
  return {
    input_tokens: 1,
    output_tokens: 1,
    usd: 0.5,
    usd_low: 0.5,
    usd_high: 1.5,
    model: "m",
    cached: false,
    within_budget: true,
    glossary_extraction: extraction,
    book_usd_low: 0.62,
    book_usd_high: 2.07,
    ...overrides,
  };
}

const noop = () => {};

function render(e: EstimateResponse, preparationMessage: string | null = null): string {
  return renderToStaticMarkup(
    <EstimateSummary
      estimate={e}
      preparationMessage={preparationMessage}
      onStart={noop}
      onContinue={noop}
    />,
  );
}

describe("EstimateSummary", () => {
  it("prices the whole book, glossary preparation included", () => {
    const html = render(estimate());
    expect(html).toContain("Costo estimado del libro: $0.62 – $2.07");
    expect(html).toContain("Incluye la preparación del glosario: $0.12 – $0.57, 57 llamadas");
  });

  it("offers no way to skip the glossary, and starting it starts the translation", () => {
    const html = render(estimate());
    expect(html).not.toContain("Omitir");
    expect(html).not.toContain("Preparar glosario");
    expect(html).toContain("Comenzar la traducción</button>");
    expect(html).toContain(
      "Traducir el libro empieza por preparar su glosario: es el primer paso pago.",
    );
  });

  it("continues to the glossary once it is prepared", () => {
    const html = render(estimate({ glossary_extraction: null, book_usd_low: 0.5, book_usd_high: 1.5 }));
    expect(html).toContain("Costo estimado del libro: $0.50 – $1.50");
    expect(html).not.toContain("Incluye la preparación del glosario");
    expect(html).toContain("Continuar al glosario</button>");
    expect(html).not.toContain("Comenzar la traducción");
  });

  it("warns when the book does not fit the budget", () => {
    expect(render(estimate({ within_budget: false }))).toContain(
      "Esta estimación supera el presupuesto configurado.",
    );
    expect(render(estimate())).not.toContain("supera el presupuesto");
  });

  it("shows why the estimate screen came back", () => {
    expect(render(estimate(), "La preparación se detuvo.")).toContain(
      '<p role="alert">La preparación se detuvo.</p>',
    );
  });
});
