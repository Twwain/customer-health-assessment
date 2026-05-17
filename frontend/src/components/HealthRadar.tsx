import {
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
  ResponsiveContainer,
} from "recharts";
import { DimensionScore } from "../api";

export default function HealthRadar({ dimensions }: { dimensions: DimensionScore[] }) {
  const data = dimensions.map((d) => ({
    dimension: d.name,
    score: d.score,
    max: d.max_score,
  }));

  return (
    <div className="w-full h-80">
      <ResponsiveContainer>
        <RadarChart data={data} cx="50%" cy="50%" outerRadius="75%">
          <PolarGrid stroke="#e2e8f0" strokeWidth={1} />
          <PolarAngleAxis
            dataKey="dimension"
            tick={{ fontSize: 13, fontWeight: 500, fill: "#475569" }}
          />
          <PolarRadiusAxis
            angle={90}
            domain={[0, 'dataMax + 3']}
            tick={{ fontSize: 10, fill: "#94a3b8" }}
            axisLine={false}
            tickCount={6}
          />
          <Radar
            name="满分"
            dataKey="max"
            stroke="#cbd5e1"
            fill="#f1f5f9"
            fillOpacity={0.3}
            strokeWidth={1}
            strokeDasharray="4 4"
            dot={false}
          />
          <Radar
            name="得分"
            dataKey="score"
            stroke="#d97706"
            fill="#d97706"
            fillOpacity={0.15}
            strokeWidth={2.5}
            dot={{ r: 4, fill: "#d97706", stroke: "#fff", strokeWidth: 2 }}
          />
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
}
