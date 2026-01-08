import React, { useState, useEffect, useRef, useCallback } from 'react';
import config from './config';

// CRITICAL: Define all reusable components OUTSIDE the main component
// This prevents them from being recreated on every render, which causes input focus loss

const SearchTermsList = React.memo(({ thresholds, onRemove }) => {
  const scrollContainerRef = useRef(null);
  const scrollPositionRef = useRef(0);

  useEffect(() => {
    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollTop = scrollPositionRef.current;
    }
  });

  const handleScroll = (e) => {
    scrollPositionRef.current = e.target.scrollTop;
  };

  return (
    <div
      ref={scrollContainerRef}
      onScroll={handleScroll}
      className="mb-6 max-h-64 overflow-y-auto"
    >
      {Object.entries(thresholds || {}).map(([term, price], index, array) => (
        <div
          key={term}
          className="flex items-center justify-between gap-3 py-3"
          style={{
            borderBottom: index < array.length - 1 ? '2px solid #CBD5E0' : 'none'
          }}
        >
          <div
            className="text-sm font-bold flex-1"
            style={{
              color: '#2D3748'
            }}
          >
            {term.toUpperCase()}
          </div>
          <div
            className="text-lg font-bold flex-shrink-0"
            style={{
              color: '#667eea',
              minWidth: '80px',
              textAlign: 'center'
            }}
          >
            ${price}
          </div>
          <button
            onClick={() => onRemove(term)}
            className="font-bold text-sm flex-shrink-0"
            style={{
              background: '#F56565',
              color: 'white',
              border: 'none',
              width: '36px',
              height: '36px',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center'
            }}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
});

// Define PixelBox outside main component
const PixelBox = React.memo(({ children, className = "", color = "#4A5568" }) => (
  <div className={className} style={{
    background: 'white',
    boxShadow: `
      0 0 0 3px ${color},
      3px 0 0 3px ${color},
      -3px 0 0 3px ${color},
      0 3px 0 3px ${color},
      0 -3px 0 3px ${color},
      6px 6px 0 0 rgba(0,0,0,0.3)
    `,
    imageRendering: 'pixelated'
  }}>
    {children}
  </div>
));

// Define PixelButton outside main component
const PixelButton = React.memo(({ children, onClick, disabled, color = "#667eea", textColor = "white", small = false }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    className={`${small ? 'px-3 py-1 text-xs' : 'px-6 py-3 text-sm'} font-bold relative`}
    style={{
      background: disabled ? '#CBD5E0' : color,
      color: textColor,
      border: 'none',
      boxShadow: disabled ? 'none' : `
        0 0 0 3px #2D3748,
        3px 0 0 3px #2D3748,
        -3px 0 0 3px #2D3748,
        0 3px 0 3px #2D3748,
        0 -3px 0 3px #2D3748,
        0 5px 0 0 #2D3748,
        0 6px 0 0 rgba(0,0,0,0.4)
      `,
      cursor: disabled ? 'not-allowed' : 'pointer',
      imageRendering: 'pixelated',
      transform: disabled ? 'none' : 'translateY(0)',
      transition: 'transform 0.1s'
    }}
    onMouseDown={(e) => !disabled && (e.currentTarget.style.transform = 'translateY(3px)')}
    onMouseUp={(e) => !disabled && (e.currentTarget.style.transform = 'translateY(0)')}
    onMouseLeave={(e) => !disabled && (e.currentTarget.style.transform = 'translateY(0)')}
  >
    {children}
  </button>
));

// Define PixelCheckbox outside main component
const PixelCheckbox = React.memo(({ checked, onChange }) => (
  <div
    onClick={onChange}
    className="w-6 h-6 cursor-pointer flex-shrink-0"
    style={{
      background: checked ? '#667eea' : 'white',
      boxShadow: `
        0 0 0 3px #2D3748,
        inset 0 0 0 3px ${checked ? '#667eea' : 'white'},
        inset 3px 3px 0 0 ${checked ? '#5A67D8' : 'rgba(0,0,0,0.1)'}
      `,
      imageRendering: 'pixelated'
    }}
  />
));

// Define PixelInput outside main component - THIS IS THE KEY FIX
const PixelInput = React.memo(({ value, onChange, placeholder, type = "text" }) => (
  <input
    type={type}
    value={value}
    onChange={onChange}
    placeholder={placeholder}
    className="w-full p-3 text-sm font-bold"
    style={{
      background: '#F7FAFC',
      color: '#2D3748',
      border: 'none',
      boxShadow: `
        0 0 0 3px #2D3748,
        inset 3px 3px 0 0 rgba(0,0,0,0.15)
      `,
      imageRendering: 'pixelated'
    }}
  />
));

const API_URL = config.API_URL;

export default function GameBoyRetreatUI() {
  const [status, setStatus] = useState({
    running: false,
    status: 'stopped',
    items_scanned_today: 0,
    matches_found_today: 0,
    recent_activity: []
  });

  const [settings, setSettings] = useState({
    platforms: { craigslist: true, offerup: true, mercari: true },
    zip_code: '95212',
    distance: 25,
    check_interval: 10,
    thresholds: {
      'gameboy': 30,
      'gba sp': 80,
      'nintendo ds': 30,
      '3ds': 110
    },
    ai_detection: true,
    strictness: 2
  });

  const [newSearch, setNewSearch] = useState({ term: '', price: '' });
  const [isLoadingSettings, setIsLoadingSettings] = useState(true);
  const [nextCheckTime, setNextCheckTime] = useState('--:--');

  // Use ref to always have latest settings
  const settingsRef = useRef(settings);
  useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);

  // Fetch initial settings only once on mount
  useEffect(() => {
    fetch(`${API_URL}/settings`)
      .then(res => res.json())
      .then(data => {
        setSettings(data);
        setIsLoadingSettings(false);
      })
      .catch(err => {
        console.error('Failed to fetch settings:', err);
        setIsLoadingSettings(false);
      });
  }, []);

  // Poll status separately from settings
  useEffect(() => {
    const interval = setInterval(() => {
      fetch(`${API_URL}/status`)
        .then(res => res.json())
        .then(data => {
          setStatus(prevStatus => {
            const activityChanged = JSON.stringify(data.recent_activity) !== JSON.stringify(prevStatus.recent_activity);
            const numbersChanged =
              data.items_scanned_today !== prevStatus.items_scanned_today ||
              data.matches_found_today !== prevStatus.matches_found_today ||
              data.status !== prevStatus.status ||
              data.running !== prevStatus.running;

            if (activityChanged || numbersChanged) {
              return data;
            }
            return prevStatus;
          });
        })
        .catch(err => console.error('Failed to fetch status:', err));
    }, 2000);

    return () => clearInterval(interval);
  }, []);

  // Calculate next check countdown
  useEffect(() => {
    if (!status.running || !status.last_check) {
      setNextCheckTime('--:--');
      return;
    }

    const timer = setInterval(() => {
      const now = new Date();
      const lastCheck = new Date();
      const [hours, minutes, seconds] = status.last_check.split(':');
      lastCheck.setHours(parseInt(hours), parseInt(minutes), parseInt(seconds));

      const nextCheck = new Date(lastCheck.getTime() + settings.check_interval * 60000);
      const diff = Math.max(0, nextCheck - now);

      const minsRemaining = Math.floor(diff / 60000);
      const secsRemaining = Math.floor((diff % 60000) / 1000);

      setNextCheckTime(`${minsRemaining}:${secsRemaining.toString().padStart(2, '0')}`);
    }, 1000);

    return () => clearInterval(timer);
  }, [status.running, status.last_check, settings.check_interval]);

  const startScraper = useCallback(() => {
    fetch(`${API_URL}/start`, { method: 'POST' })
      .then(res => res.json())
      .then(data => console.log('Started:', data))
      .catch(err => console.error('Failed to start:', err));
  }, []);

  const stopScraper = useCallback(() => {
    fetch(`${API_URL}/stop`, { method: 'POST' })
      .then(res => res.json())
      .then(data => console.log('Stopped:', data))
      .catch(err => console.error('Failed to stop:', err));
  }, []);

  // Generic settings updater
  const updateSettingField = useCallback((updates) => {
    setSettings(prev => {
      const newSettings = { ...prev, ...updates };

      fetch(`${API_URL}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newSettings)
      }).catch(err => console.error('Failed to update settings:', err));

      return newSettings;
    });
  }, []);

  const addSearchTerm = useCallback(() => {
    const trimmedTerm = newSearch.term.trim().toLowerCase();
    const parsedPrice = parseInt(newSearch.price);

    if (trimmedTerm && !isNaN(parsedPrice) && parsedPrice > 0) {
      updateSettingField({
        thresholds: {
          ...settingsRef.current.thresholds,
          [trimmedTerm]: parsedPrice
        }
      });
      setNewSearch({ term: '', price: '' });
    }
  }, [newSearch.term, newSearch.price, updateSettingField]);

  const removeSearchTerm = useCallback((term) => {
    setSettings(prev => {
      const newThresholds = { ...prev.thresholds };
      delete newThresholds[term];
      const newSettings = { ...prev, thresholds: newThresholds };

      fetch(`${API_URL}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(newSettings)
      }).catch(err => console.error('Failed to update settings:', err));

      return newSettings;
    });
  }, []);

  // Memoize ALL input change handlers to prevent de-selection
  const handleTermChange = useCallback((e) => {
    setNewSearch(prev => ({ ...prev, term: e.target.value }));
  }, []);

  const handlePriceChange = useCallback((e) => {
    setNewSearch(prev => ({ ...prev, price: e.target.value }));
  }, []);

  const handleZipChange = useCallback((e) => {
    updateSettingField({ zip_code: e.target.value });
  }, [updateSettingField]);

  const handleDistanceChange = useCallback((e) => {
    updateSettingField({ distance: parseInt(e.target.value) });
  }, [updateSettingField]);

  const handleIntervalChange = useCallback((e) => {
    updateSettingField({ check_interval: parseInt(e.target.value) });
  }, [updateSettingField]);

  const handleStrictnessChange = useCallback((e) => {
    updateSettingField({ strictness: parseInt(e.target.value) });
  }, [updateSettingField]);

  const statusColor = {
    stopped: '#718096',
    running: '#48BB78',
    error: '#F56565',
    paused: '#ECC94B'
  }[status.status] || '#718096';

  if (isLoadingSettings) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{
        background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      }}>
        <div className="text-white text-2xl font-bold">LOADING...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-8" style={{
      background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      fontFamily: 'monospace'
    }}>
      <div className="max-w-7xl mx-auto">

        {/* Header with Status Light */}
        <PixelBox className="p-6 mb-6" color="#5A67D8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold mb-2" style={{ color: '#2D3748' }}>
                MARKETPLACE SCANNER
              </h1>
              <p className="text-sm" style={{ color: '#718096' }}>
                Console Reseller Tool v1.0
              </p>
            </div>

            <div className="flex items-center gap-3">
              <div
                className="w-10 h-10"
                style={{
                  background: statusColor,
                  boxShadow: `
                    0 0 0 3px #2D3748,
                    inset 0 0 12px ${statusColor},
                    0 0 16px ${statusColor}
                  `,
                  imageRendering: 'pixelated',
                  animation: status.running ? 'pulse 2s infinite' : 'none'
                }}
              />
              <span className="text-xl font-bold" style={{ color: '#2D3748' }}>
                {status.status.toUpperCase()}
              </span>
            </div>
          </div>
        </PixelBox>

        {/* Control Buttons */}
        <PixelBox className="p-6 mb-6" color="#5A67D8">
          <div className="flex gap-4 justify-center">
            <PixelButton
              onClick={startScraper}
              disabled={status.running}
              color="#48BB78"
            >
              ▶ START
            </PixelButton>
            <PixelButton
              onClick={stopScraper}
              disabled={!status.running}
              color="#F56565"
            >
              ■ STOP
            </PixelButton>
          </div>
        </PixelBox>

        {/* Stats with View All Button */}
        <div className="grid grid-cols-2 gap-6 mb-6">
          <PixelBox color="#667eea">
            <div className="p-6">
              <div className="text-sm mb-2 font-bold" style={{ color: '#5A67D8' }}>
                {status.running ? 'NEXT CHECK IN' : 'SCRAPER INACTIVE'}
              </div>
              <div className="text-4xl font-bold mb-4" style={{ color: '#667eea' }}>
                {status.running ? nextCheckTime : '--:--'}
              </div>
              {status.running && (
                <div className="text-xs" style={{ color: '#718096' }}>
                  Checking every {settings.check_interval} minutes
                </div>
              )}
            </div>
          </PixelBox>

          <PixelBox color="#48BB78">
            <div className="p-6">
              <div className="text-sm mb-2 font-bold" style={{ color: '#38A169' }}>
                MATCHES FOUND
              </div>
              <div className="text-4xl font-bold mb-4" style={{ color: '#48BB78' }}>
                {status.matches_found_today}
              </div>
              <PixelButton
                onClick={() => console.log('Navigate to matches page')}
                color="#38A169"
              >
                VIEW ALL
              </PixelButton>
            </div>
          </PixelBox>
        </div>

        <div className="grid grid-cols-3 gap-6">
          {/* Settings Panel */}
          <PixelBox className="p-6" color="#5A67D8">
            <h2 className="text-xl font-bold mb-6" style={{ color: '#2D3748' }}>
              SETTINGS
            </h2>

            {/* Platforms */}
            <div className="mb-6">
              <div className="text-sm mb-3 font-bold" style={{ color: '#4A5568' }}>
                PLATFORMS
              </div>
              {Object.entries(settings.platforms || {}).map(([platform, enabled]) => {
                const togglePlatform = () => {
                  updateSettingField({
                    platforms: { ...settings.platforms, [platform]: !enabled }
                  });
                };

                return (
                  <div key={platform} className="flex items-center gap-3 mb-3">
                    <PixelCheckbox
                      checked={enabled}
                      onChange={togglePlatform}
                    />
                    <span
                      onClick={togglePlatform}
                      className="text-sm font-bold select-none cursor-pointer"
                      style={{
                        color: '#2D3748',
                        display: 'inline-block'
                      }}
                    >
                      {platform.toUpperCase()}
                    </span>
                  </div>
                );
              })}
            </div>

            {/* Location */}
            <div className="mb-6">
              <div className="text-sm mb-2 font-bold" style={{ color: '#4A5568' }}>
                ZIP CODE
              </div>
              <PixelInput
                value={settings.zip_code}
                onChange={handleZipChange}
              />
            </div>

            {/* Distance Slider */}
            <div className="mb-6">
              <div className="text-sm mb-3 font-bold" style={{ color: '#4A5568' }}>
                DISTANCE: {settings.distance} MI
              </div>
              <input
                type="range"
                min="5"
                max="100"
                step="5"
                value={settings.distance}
                onChange={handleDistanceChange}
                className="w-full h-8"
                style={{
                  cursor: 'grab'
                }}
              />
            </div>

            {/* Check Interval */}
            <div className="mb-6">
              <div className="text-sm mb-2 font-bold" style={{ color: '#4A5568' }}>
                CHECK EVERY
              </div>
              <select
                value={settings.check_interval}
                onChange={handleIntervalChange}
                className="w-full p-3 text-sm font-bold"
                style={{
                  background: '#F7FAFC',
                  color: '#2D3748',
                  border: 'none',
                  boxShadow: `
                    0 0 0 3px #2D3748,
                    inset 3px 3px 0 0 rgba(0,0,0,0.15)
                  `,
                  imageRendering: 'pixelated',
                  cursor: 'pointer'
                }}
              >
                <option value="5">5 MINUTES</option>
                <option value="10">10 MINUTES</option>
                <option value="15">15 MINUTES</option>
                <option value="30">30 MINUTES</option>
              </select>
            </div>

            {/* AI Detection */}
            <div className="mb-6">
              <div className="flex items-center gap-3">
                <PixelCheckbox
                  checked={settings.ai_detection}
                  onChange={() => updateSettingField({ ai_detection: !settings.ai_detection })}
                />
                <span
                  onClick={() => updateSettingField({ ai_detection: !settings.ai_detection })}
                  className="text-sm font-bold select-none cursor-pointer"
                  style={{
                    color: '#2D3748',
                    display: 'inline-block'
                  }}
                >
                  AI IMAGE DETECTION
                </span>
              </div>
            </div>

            {/* Strictness Slider */}
            <div className="mb-4">
              <div className="text-sm mb-3 font-bold" style={{ color: '#4A5568' }}>
                FILTER: {['LENIENT', 'MEDIUM', 'STRICT'][settings.strictness - 1]}
              </div>
              <input
                type="range"
                min="1"
                max="3"
                value={settings.strictness}
                onChange={handleStrictnessChange}
                className="w-full h-8"
                style={{
                  cursor: 'grab'
                }}
              />
            </div>
          </PixelBox>

          {/* Search Terms Panel */}
          <PixelBox className="p-6" color="#5A67D8">
            <h2 className="text-xl font-bold mb-6" style={{ color: '#2D3748' }}>
              SEARCH TERMS
            </h2>

            {/* Existing Terms */}
            <SearchTermsList
              thresholds={settings.thresholds}
              onRemove={removeSearchTerm}
            />

            {/* Add New Term */}
            <div className="space-y-3">
              <div className="text-sm font-bold mb-2" style={{ color: '#4A5568' }}>
                ADD NEW SEARCH
              </div>
              <PixelInput
                value={newSearch.term}
                onChange={handleTermChange}
                placeholder="SEARCH TERM"
              />
              <PixelInput
                value={newSearch.price}
                onChange={handlePriceChange}
                placeholder="MAX PRICE"
                type="number"
              />
              <PixelButton
                onClick={addSearchTerm}
                color="#667eea"
              >
                + ADD
              </PixelButton>
            </div>
          </PixelBox>

          {/* Console */}
          <PixelBox className="p-6" color="#5A67D8">
            <h2 className="text-xl font-bold mb-6" style={{ color: '#2D3748' }}>
              CONSOLE
            </h2>

            <div className="space-y-2 max-h-96 overflow-y-auto" style={{
              scrollbarWidth: 'thin',
              scrollbarColor: '#667eea #E2E8F0'
            }}>
              {status.recent_activity && status.recent_activity.length > 0 ? (
                status.recent_activity.map((activity, i) => (
                  <div
                    key={`${activity.time}-${i}`}
                    className="p-3 text-xs font-bold"
                    style={{
                      background: activity.type === 'success' ? '#C6F6D5' :
                                 activity.type === 'error' ? '#FED7D7' : '#E2E8F0',
                      color: '#2D3748',
                      boxShadow: `
                        0 0 0 2px ${activity.type === 'success' ? '#48BB78' :
                                   activity.type === 'error' ? '#F56565' : '#A0AEC0'}
                      `,
                      imageRendering: 'pixelated'
                    }}
                  >
                    <div style={{ color: '#718096', marginBottom: '4px' }}>
                      [{activity.time}]
                    </div>
                    {activity.message}
                  </div>
                ))
              ) : (
                <div className="text-center py-12 text-sm font-bold" style={{ color: '#A0AEC0' }}>
                  CONSOLE OUTPUT
                  <br />
                  <br />
                  WAITING FOR ACTIVITY...
                </div>
              )}
            </div>
          </PixelBox>
        </div>
      </div>

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.5; }
        }

        input[type="range"] {
          -webkit-appearance: none;
          appearance: none;
          width: 100%;
          height: 32px;
          background: #E2E8F0;
          outline: none;
          box-shadow: 0 0 0 3px #2D3748, inset 3px 3px 0 0 rgba(0,0,0,0.2);
        }

        input[type="range"]::-webkit-slider-thumb {
          -webkit-appearance: none;
          appearance: none;
          width: 24px;
          height: 24px;
          background: #667eea;
          cursor: grab;
          box-shadow: 0 0 0 3px #2D3748;
          border: none;
        }

        input[type="range"]::-webkit-slider-thumb:active {
          cursor: grabbing;
          background: #5A67D8;
        }

        input[type="range"]::-moz-range-thumb {
          width: 24px;
          height: 24px;
          background: #667eea;
          cursor: grab;
          border: 3px solid #2D3748;
          border-radius: 0;
        }

        input[type="range"]::-moz-range-thumb:active {
          cursor: grabbing;
          background: #5A67D8;
        }

        .overflow-y-auto::-webkit-scrollbar {
          width: 8px;
        }

        .overflow-y-auto::-webkit-scrollbar-track {
          background: #E2E8F0;
          border-radius: 0;
        }

        .overflow-y-auto::-webkit-scrollbar-thumb {
          background: #667eea;
          border-radius: 0;
          border: 2px solid #2D3748;
        }

        .overflow-y-auto::-webkit-scrollbar-thumb:hover {
          background: #5A67D8;
        }
      `}</style>
    </div>
  );
}