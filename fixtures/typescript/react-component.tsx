/**
 * @fileoverview Generic data table with sorting, filtering, pagination,
 * row selection, and async data fetching via debounced hooks.
 * @module DataTable
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

export type SortDirection = "asc" | "desc";

export interface SortState<TColumn extends string = string> { column: TColumn; direction: SortDirection }

export interface ColumnDef<TData, TColumn extends string = string> {
  id: TColumn;
  header: string;
  cell: (row: TData, index: number) => React.ReactNode;
  sortable?: boolean;
  sortFn?: (a: TData, b: TData, direction: SortDirection) => number;
  width?: number;
}

export interface PaginationState { page: number; pageSize: number; total: number }

export interface FetchResult<TData> { data: TData[]; total: number }

export interface UseDataFetchOptions<TData, TFilters extends Record<string, unknown>> {
  fetchFn: (p: { sort?: SortState; filters: TFilters; page: number; pageSize: number }, signal?: AbortSignal) => Promise<FetchResult<TData>>;
  initialFilters: TFilters;
  debounceMs?: number;
}

/* ------------------------------------------------------------------ */
/*  useDebounce                                                        */
/* ------------------------------------------------------------------ */

/** Debounces a value by `delay` ms. */
function useDebounce<T>(value: T, delay: number): T {
  const [v, set] = useState(value);
  useEffect(() => { const t = setTimeout(() => set(value), delay); return () => clearTimeout(t); }, [value, delay]);
  return v;
}

/* ------------------------------------------------------------------ */
/*  useDataFetch                                                      */
/* ------------------------------------------------------------------ */

/** Wires sorting, filtering & pagination with an abortable fetcher. Filters are debounced. */
function useDataFetch<TData, TFilters extends Record<string, unknown>>(opts: UseDataFetchOptions<TData, TFilters>) {
  const { fetchFn, initialFilters, debounceMs = 300 } = opts;
  const [sort, setSort] = useState<SortState | undefined>(undefined);
  const [filters, setFilters] = useState<TFilters>(initialFilters);
  const [pagination, setPagination] = useState<PaginationState>({ page: 1, pageSize: 20, total: 0 });
  const [data, setData] = useState<TData[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const df = useDebounce(filters, debounceMs);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let ok = false;
    (async () => {
      setLoading(true); setError(null);
      try {
        const r = await fetchFn({ sort, filters: df, page: pagination.page, pageSize: pagination.pageSize }, ctrl.signal);
        ok || (setData(r.data), setPagination(p => ({ ...p, total: r.total })));
      } catch (e: unknown) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        ok || setError(e instanceof Error ? e : new Error(String(e)));
      } finally { ok || setLoading(false); }
    })();
    return () => { ok = true; };
  }, [fetchFn, sort, df, pagination.page, pagination.pageSize]);

  return { data, loading, error, sort, setSort, filters, setFilters, pagination,
    setPage: useCallback((p: number) => setPagination(pr => ({ ...pr, page: p })), []),
    setPageSize: useCallback((s: number) => setPagination(pr => ({ ...pr, pageSize: s, page: 1 })), []),
  } as const;
}

/* ------------------------------------------------------------------ */
/*  Selection Context                                                 */
/* ------------------------------------------------------------------ */

interface SelectionCtx {
  selected: Set<string>;
  toggle: (id: string) => void;
  toggleAll: (ids: string[]) => void;
  isSelected: (id: string) => boolean;
  isAllSelected: (ids: string[]) => boolean;
}

const SelectionContext = createContext<SelectionCtx | null>(null);

function useTableSelection(): SelectionCtx {
  const ctx = useContext(SelectionContext);
  if (!ctx) throw new Error("useTableSelection must be used inside <DataTable>");
  return ctx;
}

/* ------------------------------------------------------------------ */
/*  Comparator                                                        */
/* ------------------------------------------------------------------ */

function compareValues<T>(a: T, b: T, col: string, dir: SortDirection, extract: (row: T, c: string) => unknown): number {
  const va = extract(a, col), vb = extract(b, col);
  if (va == null && vb == null) return 0;
  if (va == null) return dir === "asc" ? -1 : 1;
  if (vb == null) return dir === "asc" ? 1 : -1;
  let c: number;
  if (typeof va === "string" && typeof vb === "string") c = va.localeCompare(vb);
  else if (typeof va === "number" && typeof vb === "number") c = va - vb;
  else c = String(va).localeCompare(String(vb));
  return dir === "desc" ? -c : c;
}

/* ------------------------------------------------------------------ */
/*  DataTable                                                         */
/* ------------------------------------------------------------------ */

export interface DataTableProps<TData, TColumn extends string = string> {
  id: string;
  columns: ColumnDef<TData, TColumn>[];
  getRowId: (row: TData) => string;
  selectable?: boolean;
  sortValueExtractor?: (row: TData, col: string) => unknown;
  ariaLabel?: string;
  data: TData[];
  loading: boolean;
  error: Error | null;
  sort?: SortState;
  onSortChange: (sort: SortState | undefined) => void;
  pagination: PaginationState;
  onPageChange: (page: number) => void;
}

/** Production-grade data table with sorting, selection, pagination. */
function DataTableComponent<TData, TColumn extends string = string>(props: DataTableProps<TData, TColumn>) {
  const { id, columns, getRowId, selectable, sortValueExtractor, ariaLabel, data, loading, error, sort, onSortChange, pagination, onPageChange } = props;
  const sel = selectable ?? false;

  /* -- selection -- */
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const selCtx: SelectionCtx = useMemo(() => ({
    selected,
    toggle:    id => setSelected(p => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; }),
    toggleAll: ids => setSelected(p => ids.every(i => p.has(i)) ? new Set([...p].filter(i => !ids.includes(i))) : new Set([...p, ...ids])),
    isSelected:    id => selected.has(id),
    isAllSelected: ids => ids.length > 0 && ids.every(i => selected.has(i)),
  }), [selected]);

  /* -- sort -- */
  const sorted = useMemo(() => {
    if (!sort) return data;
    const colDef = columns.find(c => c.id === sort.column);
    const ex = sortValueExtractor ?? ((r: TData, c: string) => (r as Record<string, unknown>)[c]);
    return [...data].sort((a, b) => colDef?.sortFn ? colDef.sortFn(a, b, sort.direction) : compareValues(a, b, sort.column, sort.direction, ex));
  }, [data, sort, columns, sortValueExtractor]);

  const handleSort = useCallback((c: TColumn) => {
    if (!sort || sort.column !== c) onSortChange({ column: c, direction: "asc" });
    else if (sort.direction === "asc") onSortChange({ column: c, direction: "desc" });
    else onSortChange(undefined);
  }, [sort, onSortChange]);

  const totalPages = Math.max(1, Math.ceil(pagination.total / pagination.pageSize));
  const ids = useMemo(() => sorted.map(getRowId), [sorted, getRowId]);
  const cs = sel ? columns.length + 1 : columns.length;
  const label = ariaLabel ?? id;

  /* -- render helpers -- */
  const ariaSort = (c: TColumn) => sort?.column === c ? (sort.direction === "asc" ? "ascending" : "descending") : "none";
  const sortMark = (c: TColumn) => sort?.column !== c ? "" : (sort.direction === "asc" ? " ▲" : " ▼");
  const td = { padding: "2rem", textAlign: "center" as const };

  const body = (): React.ReactNode => {
    if (error) return <tr><td colSpan={cs} role="alert" style={{ ...td, color: "#c33" }}>{error.message}</td></tr>;
    if (loading && sorted.length === 0) return <tr><td colSpan={cs} style={td}><span role="status">Loading&hellip;</span></td></tr>;
    if (sorted.length === 0) return <tr><td colSpan={cs} style={td}>No records found.</td></tr>;
    return sorted.map((row, idx) => {
      const rid = getRowId(row);
      const ri = (pagination.page - 1) * pagination.pageSize + idx;
      return (
        <tr key={rid} aria-selected={sel ? selCtx.isSelected(rid) : undefined}>
          {sel && (
            <td role="gridcell" style={{ width: 40 }}>
              <input type="checkbox" checked={selCtx.isSelected(rid)} onChange={() => selCtx.toggle(rid)} aria-label={`Select row ${ri + 1}`} />
            </td>
          )}
          {columns.map(col => <td key={col.id} style={col.width ? { width: col.width } : undefined}>{col.cell(row, ri)}</td>)}
        </tr>
      );
    });
  };

  return (
    <SelectionContext.Provider value={selCtx}>
      <div role="region" aria-label={label} style={{ overflowX: "auto" }}>
        <table id={id} role="grid" aria-label={label} aria-busy={loading} aria-rowcount={pagination.total}
               style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr role="row">
              {sel && (
                <th role="columnheader" style={{ width: 40 }}>
                  <input type="checkbox" checked={selCtx.isAllSelected(ids)} onChange={() => selCtx.toggleAll(ids)} aria-label="Select all rows" />
                </th>
              )}
              {columns.map(col => (
                <th key={col.id} role="columnheader" aria-sort={ariaSort(col.id)} tabIndex={col.sortable ? 0 : undefined}
                    onClick={col.sortable ? () => handleSort(col.id) : undefined}
                    onKeyDown={col.sortable ? e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handleSort(col.id); } } : undefined}
                    style={{ cursor: col.sortable ? "pointer" : undefined, width: col.width, padding: "0.5rem 0.75rem", textAlign: "left", borderBottom: "2px solid #ddd" }}>
                  {col.header}{col.sortable && <span aria-hidden="true">{sortMark(col.id)}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>{body()}</tbody>
        </table>

        {pagination.total > 0 && (
          <nav aria-label="Pagination" style={{ marginTop: "0.75rem", display: "flex", gap: "0.25rem", alignItems: "center" }}>
            <span>{(pagination.page - 1) * pagination.pageSize + 1}–{Math.min(pagination.page * pagination.pageSize, pagination.total)} of {pagination.total}</span>
            <button type="button" disabled={pagination.page <= 1} onClick={() => onPageChange(pagination.page - 1)} aria-label="Previous page">&laquo;</button>
            {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
              const p = Math.max(1, Math.min(pagination.page - 2, totalPages - 4)) + i;
              if (p > totalPages) return null;
              return <button key={p} type="button" disabled={p === pagination.page} onClick={() => onPageChange(p)} aria-label={`Page ${p}`}
                             aria-current={p === pagination.page ? "page" : undefined}
                             style={{ fontWeight: p === pagination.page ? 700 : 400, minWidth: 32 }}>{p}</button>;
            })}
            <button type="button" disabled={pagination.page >= totalPages} onClick={() => onPageChange(pagination.page + 1)} aria-label="Next page">&raquo;</button>
          </nav>
        )}
      </div>
    </SelectionContext.Provider>
  );
}

const DataTable = DataTableComponent as typeof DataTableComponent & { useDataFetch: typeof useDataFetch; useTableSelection: typeof useTableSelection };
DataTable.useDataFetch = useDataFetch;
DataTable.useTableSelection = useTableSelection;

export default DataTable;
export { useDataFetch, useTableSelection, useDebounce };