import { useLayoutEffect, useRef } from "react";
import { gsap } from "gsap";

interface IngestPulseProps {
  label: string;
  value: number;
}

export function IngestPulse({ label, value }: IngestPulseProps) {
  const fillRef = useRef<HTMLSpanElement>(null);
  const sweepRef = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    const fill = fillRef.current;
    const sweep = sweepRef.current;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (!fill || !sweep || reducedMotion) return;
    const context = gsap.context(() => {
      gsap.fromTo(fill, { scaleX: 0.04 }, { scaleX: value / 100, duration: 1.15, ease: "power3.out" });
      gsap.fromTo(sweep, { xPercent: -180, opacity: 0 }, { xPercent: 180, opacity: 0.8, duration: 1.35, ease: "power2.inOut", repeat: -1, repeatDelay: 0.45 });
    });
    return () => context.revert();
  }, [value]);

  return <div aria-label={`${label}: ${value}%`} className="ingest-pulse" role="progressbar" aria-valuemax={100} aria-valuemin={0} aria-valuenow={value}>
    <span className="ingest-pulse-track"><span className="ingest-pulse-fill" ref={fillRef} /><span className="ingest-pulse-sweep" ref={sweepRef} /></span>
  </div>;
}
