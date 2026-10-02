-- ============================================================
--  Licitatii GREEN KRAFT - tabele Supabase
--  Ruleaza tot fisierul o singura data: Supabase > SQL Editor > New query > Run
-- ============================================================

-- Anunturile gasite de cautarea automata
create table if not exists lic_anunturi (
  id              text primary key,          -- cod unic (hash)
  cheie           text not null,             -- sursa|id original
  sursa_id        text not null,
  sursa           text not null,
  grup            text,
  categorie       text,
  titlu           text not null,
  link            text,
  descriere       text,
  autoritate      text,
  valoare         text,
  termen          text,
  publicat        text,
  data_publicare  date,
  judet           text,
  telefon         text,
  email           text,
  cuvant          text,
  prima_aparitie  timestamptz not null default now(),
  ultima_aparitie timestamptz not null default now()
);
create index if not exists lic_anunturi_prima_idx on lic_anunturi (prima_aparitie desc);
create index if not exists lic_anunturi_judet_idx on lic_anunturi (judet);
create index if not exists lic_anunturi_cat_idx on lic_anunturi (categorie);

-- Licitatii urmarite (favorite), comune pentru toata echipa
create table if not exists lic_favorite (
  anunt_id    text primary key references lic_anunturi(id) on delete cascade,
  nota        text,
  adaugat_de  text,
  creat_la    timestamptz not null default now()
);

-- Setari (un singur rand, id = 1)
create table if not exists lic_setari (
  id     int primary key default 1 check (id = 1),
  date   jsonb not null default '{}'::jsonb,
  modificat_la timestamptz not null default now()
);
insert into lic_setari (id, date) values (1, '{
  "zile_in_urma": 60,
  "judete_notificari": [],
  "categorii_notificari": [],
  "email_activ": true,
  "email_destinatari": ["office@greenkraft.ro"],
  "cuvinte_extra": [],
  "cuvinte_excluse": []
}'::jsonb) on conflict (id) do nothing;

-- Starea fiecarei surse la ultima rulare
create table if not exists lic_surse_stare (
  sursa_id      text primary key,
  nume          text,
  grup          text,
  initiata      boolean not null default false,
  ultima_rulare timestamptz,
  rezultate     int,
  stare         text
);

-- ---------- securitate: doar utilizatorii logati din aplicatie au acces ----------
-- (cautarea automata foloseste cheia "service_role", care trece peste aceste reguli)
alter table lic_anunturi    enable row level security;
alter table lic_favorite    enable row level security;
alter table lic_setari      enable row level security;
alter table lic_surse_stare enable row level security;

drop policy if exists "logati citesc anunturi" on lic_anunturi;
create policy "logati citesc anunturi" on lic_anunturi for select to authenticated using (true);

drop policy if exists "logati gestioneaza favorite" on lic_favorite;
create policy "logati gestioneaza favorite" on lic_favorite for all to authenticated using (true) with check (true);

drop policy if exists "logati citesc setari" on lic_setari;
create policy "logati citesc setari" on lic_setari for select to authenticated using (true);
drop policy if exists "logati modifica setari" on lic_setari;
create policy "logati modifica setari" on lic_setari for update to authenticated using (true) with check (true);

drop policy if exists "logati citesc stare" on lic_surse_stare;
create policy "logati citesc stare" on lic_surse_stare for select to authenticated using (true);
