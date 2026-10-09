import React from 'react';

export function HelpTip({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="help-tip">
      <span className="title">{title}</span>
      <span>{children}</span>
    </div>
  );
}

export function InfoBox({ children }: { children: React.ReactNode }) {
  return <div className="card muted">{children}</div>;
}
