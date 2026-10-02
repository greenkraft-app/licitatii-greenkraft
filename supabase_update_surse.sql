-- ============================================================
--  Actualizare: surse adaugate din aplicatie
--  Ruleaza o singura data: Supabase > SQL Editor > New query > Run
-- ============================================================

create table if not exists lic_surse_extra (
  id             bigint generated always as identity primary key,
  nume           text not null,
  url            text not null,            -- una sau mai multe adrese, cate una pe rand
  grup           text default '7. Adaugate din aplicatie',
  filtru_cuvinte boolean not null default false,
  js             boolean not null default false,
  activ          boolean not null default true,
  adaugat_de     text,
  creat_la       timestamptz not null default now()
);

alter table lic_surse_extra enable row level security;
drop policy if exists "logati gestioneaza surse" on lic_surse_extra;
create policy "logati gestioneaza surse" on lic_surse_extra
  for all to authenticated using (true) with check (true);

-- aplicatia poate sterge din starea surselor o sursa adaugata si apoi stearsa
drop policy if exists "logati sterg stare" on lic_surse_stare;
create policy "logati sterg stare" on lic_surse_stare for delete to authenticated using (true);

notify pgrst, 'reload schema';
