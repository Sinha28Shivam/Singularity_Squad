import { useEffect } from "react";

/** Page title (h1), short description, optional primary action. Also sets document.title. */
export default function PageHeader({ title, description, actions, children }) {
  useEffect(() => { document.title = `${title} · WorkplaceOps AI`; }, [title]);
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {description && <p className="description">{description}</p>}
        {children}
      </div>
      {actions && <div className="row">{actions}</div>}
    </header>
  );
}
