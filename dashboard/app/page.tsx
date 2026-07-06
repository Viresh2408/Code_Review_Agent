"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  FolderGit2,
  Search,
  Sparkles,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const AUTH_HEADERS = {
  Authorization: "Bearer dev-token",
};

interface Repository {
  id: number;
  github_repo_id: number;
  owner: string;
  name: string;
  full_name: string;
  installed_at: string | null;
  cumulative_debt_score: number;
  review_count: number;
  total_findings: number;
}

export default function Home() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState<string>("");

  useEffect(() => {
    async function fetchRepos() {
      try {
        const res = await fetch(`${API_BASE}/api/v1/repos`, {
          headers: AUTH_HEADERS,
        });
        if (!res.ok) {
          throw new Error(`Failed to fetch repositories (status ${res.status})`);
        }
        const data = await res.json();
        setRepos(data.repos || []);
      } catch (err: any) {
        console.error(err);
        setError(err.message || "An unexpected error occurred.");
      } finally {
        setLoading(false);
      }
    }
    fetchRepos();
  }, []);

  const filteredRepos = repos.filter((r) =>
    r.full_name.toLowerCase().includes(searchTerm.toLowerCase())
  );

  return (
    <div className="min-h-screen bg-[#090d16] text-[#e2e8f0] font-sans antialiased">
      {/* Premium Gradient Background */}
      <div className="absolute top-0 left-0 right-0 h-[320px] bg-gradient-to-b from-[#1e1b4b]/30 via-[#0f172a]/10 to-transparent pointer-events-none" />

      <div className="relative max-w-6xl mx-auto px-6 py-12">
        {/* Header */}
        <div className="flex flex-col md:flex-row md:items-center md:justify-between border-b border-[#1e293b]/50 pb-8 mb-10">
          <div className="flex items-center gap-3">
            <span className="bg-gradient-to-r from-indigo-500 to-purple-600 text-white p-2.5 rounded-xl shadow-lg shadow-indigo-500/20">
              <Activity className="h-7 w-7" />
            </span>
            <div>
              <h1 className="text-3xl font-extrabold tracking-tight bg-gradient-to-r from-white via-slate-100 to-slate-400 bg-clip-text text-transparent">
                Code Review Intelligence
              </h1>
              <p className="text-xs text-[#94a3b8] mt-1 font-medium">
                AI-powered code review and technical debt analytics
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2 mt-4 md:mt-0 bg-[#0b1329]/60 border border-[#1e293b]/50 rounded-xl px-3 py-1 text-xs text-[#94a3b8]">
            <Sparkles className="h-4 w-4 text-indigo-400 animate-pulse" />
            <span>Developer Mode Active</span>
          </div>
        </div>

        {/* Info/Introduction Banner */}
        <div className="bg-gradient-to-r from-[#10b981]/5 via-[#6366f1]/5 to-[#a855f7]/5 border border-indigo-500/20 rounded-2xl p-6 mb-10 flex flex-col md:flex-row items-start md:items-center gap-5 justify-between">
          <div className="space-y-1">
            <h3 className="text-sm font-bold text-white tracking-wide uppercase">Technical Debt Monitoring</h3>
            <p className="text-xs text-[#94a3b8] max-w-xl">
              Track code complexity metrics, code duplication, test coverage, and automated AI security comments across your repositories in real-time.
            </p>
          </div>
          <Link
            href="/repos/1"
            className="flex items-center gap-1.5 px-4 py-2.5 bg-indigo-600/10 border border-indigo-500/30 hover:bg-indigo-600/20 text-indigo-300 rounded-xl text-xs font-bold transition-all duration-300 group"
          >
            <span>View Default Repo</span>
            <ArrowRight className="h-4 w-4 transition-transform duration-300 group-hover:translate-x-1" />
          </Link>
        </div>

        {/* Main Controls & Listing */}
        <div className="mb-6 flex flex-col md:flex-row justify-between items-start md:items-center gap-4">
          <div>
            <h2 className="text-lg font-bold text-white">Monitored Repositories</h2>
            <p className="text-xs text-[#64748b]">Select a repository to explore its technical debt score progression</p>
          </div>

          {/* Search bar */}
          <div className="relative w-full md:w-[280px]">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-[#64748b]" />
            <input
              type="text"
              placeholder="Search repositories..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="bg-[#0b1329]/60 border border-[#1e293b] text-xs text-white rounded-xl pl-9 pr-4 py-2.5 w-full focus:outline-none focus:border-indigo-500 transition-all duration-300"
            />
          </div>
        </div>

        {/* Loading and States */}
        {loading ? (
          <div className="flex flex-col items-center justify-center py-20 bg-[#0b1329]/20 border border-[#1e293b]/40 rounded-2xl">
            <div className="animate-spin rounded-full h-10 w-10 border-t-2 border-b-2 border-indigo-500 mb-4" />
            <p className="text-xs text-[#94a3b8] animate-pulse">Retrieving repository index...</p>
          </div>
        ) : error ? (
          <div className="bg-rose-950/20 border border-rose-500/30 rounded-2xl p-8 text-center">
            <AlertTriangle className="h-12 w-12 text-rose-500 mx-auto mb-3" />
            <h3 className="text-base font-bold text-rose-400">Connection to API Failed</h3>
            <p className="text-xs text-[#94a3b8] mt-1 max-w-md mx-auto">{error}</p>
            <button
              onClick={() => window.location.reload()}
              className="mt-4 px-4 py-2 bg-rose-500/10 border border-rose-500/30 hover:bg-rose-500/20 text-rose-300 rounded-xl text-xs font-bold transition-all duration-300"
            >
              Retry Connection
            </button>
          </div>
        ) : filteredRepos.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 bg-[#0b1329]/20 border border-dashed border-[#1e293b]/50 rounded-2xl text-center">
            <FolderGit2 className="h-12 w-12 text-[#475569] mb-3" />
            <p className="text-sm text-[#94a3b8] font-bold">No Repositories Installed</p>
            <p className="text-xs text-[#64748b] mt-1 max-w-xs">
              Install the GitHub App or hook up a repository to receive automated reviews.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {filteredRepos.map((repo) => (
              <Link
                key={repo.id}
                href={`/repos/${repo.id}`}
                className="group relative bg-[#0b1329]/40 backdrop-blur-md border border-[#1e293b]/50 hover:border-indigo-500/40 rounded-2xl p-6 transition-all duration-300 flex flex-col justify-between hover:-translate-y-0.5"
              >
                <div>
                  <div className="flex items-center justify-between mb-4">
                    <span className="text-xs text-[#64748b] font-mono">ID: {repo.github_repo_id}</span>
                    <span className="flex items-center gap-1 text-[10px] uppercase font-bold tracking-wider px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                      Active
                    </span>
                  </div>
                  <h3 className="text-lg font-bold text-white group-hover:text-indigo-400 transition-colors duration-300 mb-1">
                    {repo.full_name}
                  </h3>
                  <p className="text-[11px] text-[#64748b] mb-4">
                    Registered: {repo.installed_at ? new Date(repo.installed_at).toLocaleDateString() : "Unknown"}
                  </p>
                </div>

                <div className="grid grid-cols-3 gap-2 border-t border-[#1e293b]/60 pt-4 mt-2">
                  <div className="space-y-0.5">
                    <span className="text-[10px] uppercase font-bold tracking-wider text-[#64748b] block">Debt Score</span>
                    <div className="flex items-baseline gap-1">
                      <span className="text-base font-extrabold text-white">{repo.cumulative_debt_score}</span>
                      <span className="text-[9px] text-[#64748b] font-semibold">pts</span>
                    </div>
                  </div>
                  <div className="space-y-0.5">
                    <span className="text-[10px] uppercase font-bold tracking-wider text-[#64748b] block">Reviews</span>
                    <span className="text-base font-extrabold text-white">{repo.review_count}</span>
                  </div>
                  <div className="space-y-0.5">
                    <span className="text-[10px] uppercase font-bold tracking-wider text-[#64748b] block">Findings</span>
                    <span className="text-base font-extrabold text-white">{repo.total_findings}</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
