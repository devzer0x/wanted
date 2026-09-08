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
    <div className="flex flex-col gap-4 pb-20 pt-4 lg:pb-4">
      <header className="flex flex-col gap-2">
        <h1 className="wordmark text-4xl sm:text-5xl" data-text="PREDICT">
          PREDICT
        </h1>
        <p className="max-w-2xl text-sm text-smoke">
          Predictions settle from the same telemetry this site reads — never from a guess about
          what probably happened. Free to enter; correct calls earn TTWO.
        </p>
      </header>
      <PredictView />
    </div>
  );
}
