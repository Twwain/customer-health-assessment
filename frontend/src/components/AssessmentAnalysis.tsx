import type { AssessmentSection } from "../lib/assessmentPresentation";

export function AssessmentAnalysis({ sections }: { sections: AssessmentSection[] }) {
  return <>{sections.map((section) => <section key={section.title}>
    <h4>{section.title}</h4>
    <ul className="list-disc">{section.items.map((item, index) => <li key={index} className="whitespace-pre-wrap">
      {item.text}
      {item.detail && <div className="mt-1 text-muted">{item.detail}</div>}
    </li>)}</ul>
  </section>)}</>;
}
