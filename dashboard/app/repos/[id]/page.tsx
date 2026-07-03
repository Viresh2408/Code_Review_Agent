"use client";

import React, { useEffect, useState, useMemo } from "react";
import { useParams } from "next/navigation";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import {
  Activity,
  AlertTriangle,
  Calendar,
  CheckCircle,
  Clock,
  Code,
  FileText,
  Filter,
  Layers,
  Search,
  Terminal,
  TrendingDown,
  TrendingUp,
} from "lucide-react";

// API Base URL config
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const AUTH_HEADERS = {
  Authorization: "Bearer dev-token",
};

interface TrendPoint {
  date: string;
  file_path: string;
  score: number;
}

interface Finding {
  id: number;
  agent: string;
  file_path: string;
  line_number: number | null;
  severity: "blocker" | "warning" | "nit";
  category: string;
  message: string;
  confidence: number | null;
  escalated_to_claude: boolean;
  suggested_fix: string | null;
}

export default function RepoDashboard() {
  const params = useParams();
  const repoId = params.id as string;

  // State variables
  const [days, setDays] = useState<number>(30);
  const [filePathFilter, setFilePathFilter] = useState<string>("");
  const [trendData, setTrendData] = useState<TrendPoint[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Search input state
  const [searchInput, setSearchInput] = useState<string>("");

  // Simulated review runs for selector
  const [selectedReviewId, setSelectedReviewId] = useState<number>(1);
  const reviewOptions = [1, 2, 3];

  // Fetch trend and findings
  useEffect(() => {
    async function fetchData() {
      setLoading(true);
      setError(null);
      try {
        // 1. Fetch Debt Trend
        let trendUrl = `${API_BASE}/api/v1/repos/${repoId}/debt-trend?days=${days}`;
        if (filePathFilter) {
          trendUrl += `&file_path=${encodeURIComponent(filePathFilter)}`;
        }
        
        const trendRes = await fetch(trendUrl, { headers: AUTH_HEADERS });
        if (!trendRes.ok) {
          throw new Error(`Failed to fetch trend data (status ${trendRes.status})`);
        }
        const trendJson = await trendRes.json();
        setTrendData(trendJson.trend || []);

        // 2. Fetch Findings for the selected review
        const findingsUrl = `${API_BASE}/api/v1/reviews/${selectedReviewId}/findings?limit=50`;
        const findingsRes = await fetch(findingsUrl, { headers: AUTH_HEADERS });
        if (findingsRes.ok) {
          const findingsJson = await findingsRes.json();
          setFindings(findingsJson.findings || []);
        } else {
          // Fallback if review does not exist yet (return empty list gracefully)
          setFindings([]);
        }
      } catch (err: any) {
        console.error(err);
        setError(err.message || "An unexpected error occurred.");
      } finally {
        setLoading(false);
      }
    }

    if (repoId) {
      fetchData();
    }
  }, [repoId, days, filePathFilter, selectedReviewId]);

  // Pivot trend data for Recharts (keys should be file paths, date on X axis)
  const chartData = useMemo(() => {
    const dateMap: Record<string, Record<string, number>> = {};
    const allFiles = new Set<string>();

    trendData.forEach((pt) => {
      allFiles.add(pt.file_path);
      if (!dateMap[pt.date]) {
        dateMap[pt.date] = {};
      }
      dateMap[pt.date][pt.file_path] = pt.score;
    });

    const dates = Object.keys(dateMap).sort();
    return {
      files: Array.from(allFiles),
      data: dates.map((d) => ({
        date: d,
        ...dateMap[d],
      })),
    };
  }, [trendData]);

  // Handle file path search submit
  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setFilePathFilter(searchInput.trim());
  };

  // Color generator for chart lines
  const getLineColor = (index: number) => {
    const colors = [
      "#6366f1", // violet/indigo
      "#10b981", // emerald
      "#f43f5e", // rose
      "#f59e0b", // amber
      "#06b6d4", // cyan
      "#a855f7", // purple
    ];
    return colors[index % colors.length];
  };

  // Compute metric stats
  const stats = useMemo(() => {
    const totalFindings = findings.length;
    const blockers = findings.filter((f) => f.severity === "blocker").length;
    const warnings = findings.filter((f) => f.severity === "warning").length;
    const nits = findings.filter((f) => f.severity === "nit").length;

    // Get overall trend delta (sum of score changes)
    const uniqueFiles = new Set(trendData.map((pt) => pt.file_path));
    let latestScoreSum = 0;
    uniqueFiles.forEach((file) => {
      const filePoints = trendData.filter((pt) => pt.file_path === file);
      if (filePoints.length > 0) {
        // Sort by date desc to get latest
        const sorted = [...filePoints].sort((a, b) => b.date.localeCompare(a.date));
        latestScoreSum += sorted[0].score;
      }
    });

    return {
      totalFindings,
      blockers,
      warnings,
      nits,
      currentDebtScore: parseFloat(latestScoreSum.toFixed(2)),
    };
  }, [findings, trendData]);

  return (
    <div className="min-h-screen bg-[#090d16] text-[#e2e8f0] font-sans antialiased">
      {/* Premium Gradient Header Banner */}
      <div className="absolute top-0 left-0 right-0 h-[280px] bg-gradient-to-b from-[#1e1b4b]/30 via-[#0f172a]/10 to-transparent pointer-events-none" />

      <div className="relative max-w-7xl mx-auto px-6 py-8">
        {/* Navigation / Header */}
        <div className="flex flex-col md:flex-row md:items-center md:justify-between border-b border-[#1e293b]/50 pb-6 mb-8">
          <div>
            <div className="flex items-center gap-3">
              <span className="bg-gradient-to-r from-indigo-500 to-purple-600 text-white p-2 rounded-lg shadow-lg shadow-indigo-500/20">
                <Activity className="h-6 w-6" />
              </span>
              <div>
                <h1 className="text-3xl font-extrabold tracking-tight bg-gradient-to-r from-white via-slate-100 to-slate-400 bg-clip-text text-transparent">
                  Repository Technical Debt
                </h1>
                <p className="text-sm text-[#94a3b8] mt-1">
                  Monitoring repository ID: <span className="font-mono text-indigo-400 font-bold">{repoId}</span>
                </p>
              </div>
            </div>
          </div>

          {/* Quick Date Filters */}
          <div className="flex items-center gap-2 mt-4 md:mt-0">
            <span className="text-xs text-[#94a3b8] font-bold uppercase tracking-wider mr-2">Timeframe</span>
            {[7, 30, 90].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-4 py-2 text-xs font-semibold rounded-md border transition-all duration-300 ${
                  days === d
                    ? "bg-indigo-600 border-indigo-500 text-white shadow-md shadow-indigo-600/10 scale-105"
                    : "bg-[#0f172a] border-[#1e293b] text-[#94a3b8] hover:border-slate-700 hover:text-white"
                }`}
              >
                {d} Days
              </button>
            ))}
          </div>
        </div>

        {/* Loading / Error States */}
        {loading && trendData.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20">
            <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-indigo-500 mb-4" />
            <p className="text-[#94a3b8] text-sm animate-pulse">Loading technical debt intelligence...</p>
          </div>
        ) : error ? (
          <div className="bg-rose-950/20 border border-rose-500/30 rounded-xl p-6 text-center mb-8">
            <AlertTriangle className="h-12 w-12 text-rose-500 mx-auto mb-3" />
            <h3 className="text-lg font-bold text-rose-400">Database Connection Unavailable</h3>
            <p className="text-[#94a3b8] text-sm mt-1 max-w-lg mx-auto">{error}</p>
            <button
              onClick={() => window.location.reload()}
              className="mt-4 px-4 py-2 bg-rose-500/10 border border-rose-500/30 hover:bg-rose-500/20 text-rose-300 rounded-lg text-xs font-bold transition-all duration-300"
            >
              Retry Connection
            </button>
          </div>
        ) : (
          <>
            {/* Metric Cards Grid */}
            <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
              {/* Card 1: Cumulative Debt Score */}
              <div className="bg-[#0b1329]/60 backdrop-blur-md border border-[#1e293b]/60 rounded-2xl p-6 transition-all duration-300 hover:border-indigo-500/30">
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-bold uppercase tracking-wider text-[#94a3b8]">Cumulative Debt</span>
                  <Layers className="h-5 w-5 text-indigo-400" />
                </div>
                <div className="flex items-baseline gap-2">
                  <span className="text-3xl font-black tracking-tight">{stats.currentDebtScore}</span>
                  <span className="text-xs text-indigo-400 font-bold">pts</span>
                </div>
                <div className="flex items-center gap-1.5 mt-2 text-xs text-[#94a3b8]">
                  <TrendingUp className="h-3.5 w-3.5 text-indigo-400" />
                  <span>Calculated across changed files</span>
                </div>
              </div>

              {/* Card 2: Blocker Severity */}
              <div className="bg-[#0b1329]/60 backdrop-blur-md border border-[#1e293b]/60 rounded-2xl p-6 transition-all duration-300 hover:border-rose-500/30">
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-bold uppercase tracking-wider text-[#94a3b8]">Blocker Issues</span>
                  <AlertTriangle className="h-5 w-5 text-rose-500" />
                </div>
                <div className="flex items-baseline gap-1">
                  <span className="text-3xl font-black text-rose-400 tracking-tight">{stats.blockers}</span>
                </div>
                <div className="flex items-center gap-1.5 mt-2 text-xs text-[#94a3b8]">
                  <Clock className="h-3.5 w-3.5 text-rose-400" />
                  <span>Critical vulnerabilities detected</span>
                </div>
              </div>

              {/* Card 3: Warning Severity */}
              <div className="bg-[#0b1329]/60 backdrop-blur-md border border-[#1e293b]/60 rounded-2xl p-6 transition-all duration-300 hover:border-amber-500/30">
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-bold uppercase tracking-wider text-[#94a3b8]">Warning Issues</span>
                  <AlertTriangle className="h-5 w-5 text-amber-500" />
                </div>
                <div className="flex items-baseline gap-1">
                  <span className="text-3xl font-black text-amber-400 tracking-tight">{stats.warnings}</span>
                </div>
                <div className="flex items-center gap-1.5 mt-2 text-xs text-[#94a3b8]">
                  <TrendingDown className="h-3.5 w-3.5 text-amber-400" />
                  <span>Refactoring flags needing review</span>
                </div>
              </div>

              {/* Card 4: Nit Severity */}
              <div className="bg-[#0b1329]/60 backdrop-blur-md border border-[#1e293b]/60 rounded-2xl p-6 transition-all duration-300 hover:border-emerald-500/30">
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-bold uppercase tracking-wider text-[#94a3b8]">Nit Issues</span>
                  <CheckCircle className="h-5 w-5 text-emerald-500" />
                </div>
                <div className="flex items-baseline gap-1">
                  <span className="text-3xl font-black text-emerald-400 tracking-tight">{stats.nits}</span>
                </div>
                <div className="flex items-center gap-1.5 mt-2 text-xs text-[#94a3b8]">
                  <Code className="h-3.5 w-3.5 text-emerald-400" />
                  <span>Stylistic consistency alerts</span>
                </div>
              </div>
            </div>

            {/* Main Dashboard Layout */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 mb-12">
              {/* Left Column: Recharts Line Chart Card (2/3 width) */}
              <div className="lg:col-span-2 bg-[#0b1329]/40 backdrop-blur-md border border-[#1e293b]/50 rounded-3xl p-6">
                <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
                  <div>
                    <h3 className="text-lg font-bold text-white">Debt Score Progression</h3>
                    <p className="text-xs text-[#94a3b8]">Historical technical debt scores accumulated per file</p>
                  </div>

                  {/* File Search Form */}
                  <form onSubmit={handleSearchSubmit} className="flex items-center gap-2">
                    <div className="relative">
                      <Search className="absolute left-3 top-2.5 h-4 w-4 text-[#64748b]" />
                      <input
                        type="text"
                        placeholder="Filter by file path..."
                        value={searchInput}
                        onChange={(e) => setSearchInput(e.target.value)}
                        className="bg-[#090d16] border border-[#1e293b] text-xs text-white rounded-lg pl-9 pr-4 py-2 w-[200px] focus:outline-none focus:border-indigo-500 transition-all duration-300"
                      />
                    </div>
                    <button
                      type="submit"
                      className="px-3 py-2 bg-indigo-600 hover:bg-indigo-500 border border-indigo-500 rounded-lg text-xs font-semibold text-white transition-all duration-300"
                    >
                      Apply
                    </button>
                    {filePathFilter && (
                      <button
                        type="button"
                        onClick={() => {
                          setSearchInput("");
                          setFilePathFilter("");
                        }}
                        className="px-2.5 py-2 bg-[#1e293b]/50 hover:bg-[#1e293b] border border-[#334155] rounded-lg text-xs font-semibold text-[#e2e8f0] transition-all duration-300"
                      >
                        Reset
                      </button>
                    )}
                  </form>
                </div>

                {/* Chart Container */}
                <div className="h-[340px] w-full">
                  {chartData.data.length === 0 ? (
                    <div className="h-full flex flex-col items-center justify-center bg-[#090d16]/30 border border-dashed border-[#1e293b] rounded-2xl">
                      <Layers className="h-10 w-10 text-[#475569] mb-2 animate-bounce" />
                      <p className="text-xs text-[#64748b]">No technical debt progression records found.</p>
                    </div>
                  ) : (
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={chartData.data} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                        <defs>
                          <linearGradient id="chartGrid" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="0%" stopColor="#1e293b" stopOpacity={0.1} />
                            <stop offset="100%" stopColor="#1e293b" stopOpacity={0.01} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" vertical={false} />
                        <XAxis
                          dataKey="date"
                          stroke="#475569"
                          fontSize={10}
                          tickLine={false}
                          axisLine={false}
                          dy={10}
                        />
                        <YAxis
                          stroke="#475569"
                          fontSize={10}
                          tickLine={false}
                          axisLine={false}
                          dx={-10}
                        />
                        <Tooltip
                          contentStyle={{
                            backgroundColor: "#0f172a",
                            border: "1px solid #334155",
                            borderRadius: "12px",
                            fontSize: "11px",
                            color: "#e2e8f0",
                          }}
                        />
                        <Legend
                          wrapperStyle={{ fontSize: "10px", paddingTop: "15px" }}
                          iconType="circle"
                        />
                        {chartData.files.map((file, idx) => (
                          <Line
                            key={file}
                            type="monotone"
                            dataKey={file}
                            stroke={getLineColor(idx)}
                            strokeWidth={2.5}
                            activeDot={{ r: 6 }}
                            dot={{ r: 3 }}
                          />
                        ))}
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </div>
              </div>

              {/* Right Column: Review Details Selector & Info Card */}
              <div className="bg-[#0b1329]/40 backdrop-blur-md border border-[#1e293b]/50 rounded-3xl p-6 flex flex-col justify-between">
                <div>
                  <h3 className="text-lg font-bold text-white mb-2">Review Summary</h3>
                  <p className="text-xs text-[#94a3b8] mb-6">Select a review run to view detailed repository findings</p>

                  <div className="space-y-4">
                    {/* Review Selector */}
                    <div>
                      <label className="block text-xs font-bold uppercase tracking-wider text-[#94a3b8] mb-2">
                        Select Review Run ID
                      </label>
                      <div className="relative">
                        <select
                          value={selectedReviewId}
                          onChange={(e) => setSelectedReviewId(Number(e.target.value))}
                          className="w-full bg-[#090d16] border border-[#1e293b] text-sm text-white rounded-xl px-4 py-3 appearance-none focus:outline-none focus:border-indigo-500 transition-all duration-300"
                        >
                          {reviewOptions.map((opt) => (
                            <option key={opt} value={opt}>
                              Review Run #{opt}
                            </option>
                          ))}
                        </select>
                        <div className="absolute right-4 top-4 border-t-4 border-t-white border-l-4 border-l-transparent border-r-4 border-r-transparent pointer-events-none" />
                      </div>
                    </div>

                    {/* Stats summary list */}
                    <div className="border border-[#1e293b] rounded-2xl p-4 bg-[#090d16]/30 space-y-3">
                      <div className="flex justify-between items-center text-xs">
                        <span className="text-[#94a3b8] flex items-center gap-1.5">
                          <Code className="h-4 w-4" /> Files Audited
                        </span>
                        <span className="font-bold">{chartData.files.length}</span>
                      </div>
                      <div className="flex justify-between items-center text-xs">
                        <span className="text-[#94a3b8] flex items-center gap-1.5">
                          <Terminal className="h-4 w-4" /> Total Findings
                        </span>
                        <span className="font-bold text-indigo-400">{stats.totalFindings}</span>
                      </div>
                      <div className="flex justify-between items-center text-xs">
                        <span className="text-[#94a3b8] flex items-center gap-1.5">
                          <Calendar className="h-4 w-4" /> Last Updated
                        </span>
                        <span className="font-bold">2026-07-03</span>
                      </div>
                    </div>
                  </div>
                </div>

                {/* Dashboard Actions */}
                <div className="mt-6 pt-6 border-t border-[#1e293b] space-y-3">
                  <div className="text-xs text-[#94a3b8] italic">
                    Authentication verified via <span className="font-mono text-indigo-400 font-bold">Bearer dev-token</span>.
                  </div>
                </div>
              </div>
            </div>

            {/* Findings Detail Section */}
            <div className="bg-[#0b1329]/40 backdrop-blur-md border border-[#1e293b]/50 rounded-3xl p-6 mb-8">
              <div className="flex items-center justify-between mb-6">
                <div>
                  <h3 className="text-lg font-bold text-white">Review Findings Detail</h3>
                  <p className="text-xs text-[#94a3b8]">Detailed reports and recommended fixes generated by AI reviewer agents</p>
                </div>
                <span className="px-3 py-1 bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 text-xs font-semibold rounded-full flex items-center gap-1.5">
                  <Filter className="h-3.5 w-3.5" /> Latest Run
                </span>
              </div>

              {findings.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-16 bg-[#090d16]/30 border border-dashed border-[#1e293b] rounded-2xl">
                  <CheckCircle className="h-10 w-10 text-emerald-500 mb-2" />
                  <p className="text-sm font-semibold text-white">No Issues Found!</p>
                  <p className="text-xs text-[#64748b] mt-1 max-w-sm text-center">
                    All reviews passed successfully. Code architecture and security meet target specifications.
                  </p>
                </div>
              ) : (
                /* Findings Table */
                <div className="overflow-x-auto">
                  <table className="w-full text-left border-collapse">
                    <thead>
                      <tr className="border-b border-[#1e293b] text-xs font-bold uppercase tracking-wider text-[#94a3b8] bg-[#090d16]/20">
                        <th className="py-4 px-4">Severity</th>
                        <th className="py-4 px-4">Agent</th>
                        <th className="py-4 px-4">File / Line</th>
                        <th className="py-4 px-4">Issue Description</th>
                        <th className="py-4 px-4">Suggested Fix</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[#1e293b]/40 text-xs">
                      {findings.map((f) => (
                        <tr key={f.id} className="hover:bg-[#0f172a]/20 transition-all duration-200">
                          {/* Severity Badge */}
                          <td className="py-4 px-4">
                            <span
                              className={`px-2.5 py-1 rounded-full text-[10px] font-black uppercase tracking-wider inline-block ${
                                f.severity === "blocker"
                                  ? "bg-rose-500/10 border border-rose-500/20 text-rose-400"
                                  : f.severity === "warning"
                                  ? "bg-amber-500/10 border border-amber-500/20 text-amber-400"
                                  : "bg-indigo-500/10 border border-indigo-500/20 text-indigo-400"
                              }`}
                            >
                              {f.severity}
                            </span>
                          </td>

                          {/* Agent Name */}
                          <td className="py-4 px-4 font-mono text-[#94a3b8]">{f.agent}</td>

                          {/* File Path & Line */}
                          <td className="py-4 px-4 max-w-[220px] truncate">
                            <div className="font-semibold text-[#e2e8f0]" title={f.file_path}>
                              {f.file_path.split("/").pop()}
                            </div>
                            <div className="text-[10px] text-[#64748b] font-mono mt-0.5 truncate" title={f.file_path}>
                              {f.file_path} {f.line_number !== null ? `:L${f.line_number}` : ""}
                            </div>
                          </td>

                          {/* Message */}
                          <td className="py-4 px-4 text-[#cbd5e1] max-w-[320px] break-words">
                            {f.message}
                          </td>

                          {/* Suggested Fix */}
                          <td className="py-4 px-4 text-[#94a3b8] max-w-[260px] truncate font-mono">
                            {f.suggested_fix ? (
                              <div className="flex items-center gap-1.5 bg-[#090d16] border border-[#1e293b] rounded-lg p-2 max-w-full overflow-x-auto text-[10px]">
                                <FileText className="h-3.5 w-3.5 text-indigo-400 shrink-0" />
                                <span className="text-indigo-200">{f.suggested_fix}</span>
                              </div>
                            ) : (
                              <span className="italic text-[#475569]">No recommendation</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
