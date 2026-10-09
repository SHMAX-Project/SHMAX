import React from 'react';
import ReactDOM from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import { NavigationSafetyProvider } from './navigation';
import App from './App';
import './i18n';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <RouterProvider router={createBrowserRouter([{ path: "*", element: <NavigationSafetyProvider><App /></NavigationSafetyProvider> }])} />
  </React.StrictMode>
);
