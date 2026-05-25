# DISCLAIMER — Shadow libraries (Anna's Archive, Sci-Hub)

## Comportement par défaut

Le plugin `paper-trail` n'active **pas** par défaut les sources de type
shadow library (Anna's Archive, Sci-Hub). Sa cascade d'acquisition de
PDFs s'arrête aux sources légales open-access et institutionnelles
(Crossref OA, arXiv, OpenAlex, Unpaywall, HAL, CORE, archive.org), puis
bascule en WebSearch queue (intervention humaine) si aucune source
légale ne livre.

Les références dont le PDF n'est pas accessible via les sources légales
restent en état `blocked_human:cascade_exhausted` pour décision humaine.

## Activation explicite (opt-in)

L'activation des shadow libraries est faite **explicitement** par
l'utilisateur via une variable d'environnement :

```bash
export RESEARCH_ENABLE_SHADOW_LIBS=1
```

Lorsqu'activée :
- La cascade ajoute `scihub_optin` et `annas_archive_optin` en sources
  9 et 10 (avant la WebSearch queue)
- Un disclaimer est affiché sur `stderr` au premier appel de chaque
  session
- Toutes les acquisitions via shadow sont tracées dans le registre
  (`acquisition_attempts[].via` préfixé `_optin`)

## Responsabilité de l'utilisateur

L'utilisateur qui active `RESEARCH_ENABLE_SHADOW_LIBS=1` reconnaît :

1. **Statut légal variable selon juridiction**

   L'accès au contenu de Anna's Archive et Sci-Hub peut violer le droit
   d'auteur dans votre juridiction. En France, en Union Européenne, aux
   États-Unis et dans la plupart des pays signataires de la Convention
   de Berne, le téléchargement de contenu protégé sans autorisation est
   illégal.

2. **Droit légal d'accès au matériel téléchargé**

   En activant cette option, vous confirmez avoir le droit légal
   d'accéder au matériel téléchargé. Cela peut inclure, selon votre
   juridiction et le contexte :
   - **Fair use / fair dealing** (citation académique, recherche)
   - **Droit de citation** (analyse critique)
   - **Accès institutionnel** (vous êtes affilié à une institution qui
     a souscrit une licence)
   - **Œuvres dans le domaine public**
   - **Œuvres dont vous êtes l'auteur ou pour lesquelles vous avez une
     licence explicite**

   La détermination du caractère légal de votre usage relève de votre
   responsabilité exclusive et dépend de votre situation juridictionnelle.

3. **Aucun contenu protégé hébergé par paper-trail**

   Ce plugin **n'héberge aucun contenu protégé**. Il agit uniquement
   comme client HTTP requêtant les services publics distants (les
   miroirs Sci-Hub, l'API Anna's Archive). Aucun PDF n'est pré-stocké
   ou distribué via le plugin.

4. **Pas d'activation automatique**

   Aucun mécanisme du plugin ne définit `RESEARCH_ENABLE_SHADOW_LIBS=1`
   automatiquement. L'activation est strictement manuelle, par
   définition explicite de la variable dans votre shell ou votre
   environnement de session.

## Désactivation

Pour désactiver de manière permanente :
- Ne définissez pas `RESEARCH_ENABLE_SHADOW_LIBS` dans votre
  environnement
- Ou définissez-la explicitement à autre chose que `1` :
  `export RESEARCH_ENABLE_SHADOW_LIBS=0`

## Cas particuliers

### Usage strictement académique

Si votre activation a vocation strictement académique (citation,
recherche, vérification de claims dans des publications scientifiques
peer-reviewed), vous opérez sous le régime du **fair use** (US) ou du
**droit de citation** (France/UE). Ces exceptions au droit d'auteur
**ne couvrent pas** la redistribution du contenu téléchargé. Le
plugin ne redistribue jamais le contenu acquis ; il le stocke
localement pour votre usage personnel.

### Œuvres orphelines, hors commerce, ou indisponibles

Pour les œuvres orphelines (auteur introuvable), hors-commerce ou
indisponibles en édition courante, certaines juridictions (notamment
en Allemagne, France via la loi Hadopi/Création) prévoient des
exceptions. Renseignez-vous sur votre cas spécifique.

### Œuvres rachetées par votre institution

Si votre institution a déjà payé l'accès à un article (via une
licence avec l'éditeur) mais que vous travaillez hors-VPN, vous pouvez
avoir le droit moral d'y accéder par d'autres moyens. C'est une zone
grise juridique ; consultez votre service de bibliothèque
universitaire.

## Logs et traçabilité

Le plugin trace dans le registre toutes les acquisitions via shadow
libraries (`acquisition_attempts[].via` = `scihub_optin` ou
`annas_archive_optin`). Cela permet, en cas d'audit, de distinguer les
PDFs acquis par voie OA légale de ceux acquis via shadow.

Les `acquisition_attempts` ne sont **jamais** redistribués ; ils
restent dans votre registre local.

## En cas de doute

**Ne pas activer** `RESEARCH_ENABLE_SHADOW_LIBS=1`. Le plugin
fonctionne parfaitement sans shadow libraries pour les sources OA, ce
qui couvre déjà ~60-70% des publications académiques modernes selon
les domaines. Pour le reste, l'état `blocked_human:cascade_exhausted`
vous permet de décider au cas par cas (acquisition via VPN
institutionnel, prêt entre bibliothèques, contact direct avec
l'auteur, etc.).
