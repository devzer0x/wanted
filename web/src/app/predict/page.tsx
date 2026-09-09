import type { Metadata } from "next";
import { PredictView } from "@/components/predict/PredictView";
import { routeMetadata } from "@/lib/metadata";

export const metadata: Metadata = routeMetadata({
  path: "/predict",
  title: "Predict",
  description: "Every open and recently settled prediction on what the agent does next.",
});

export default function PredictPage() {
  return (
    <div className="pb-16 pt-6 lg:pb-10">
      {/* The record pills belong beside this title, and they need the same fetch the cards use, so
          the header is handed to the client view rather than duplicating a request for them. */}
      <PredictView
        header={
          <div className="min-w-0">
            <h1 className="page-title panel-yellow">Predict</h1>
            <p className="mt-3 max-w-xl text-[15px] font-bold leading-relaxed text-dim">
              Predictions settle from the same telemetry this site reads — never from a guess about
              what probably happened. Free to enter; correct calls earn TTWO.
            </p>
          </div>
        }
      />
    </div>
  );
}
